import asyncio
from twisted.internet.defer import ensureDeferred, Deferred
import aiohttp
from aiohttp import web
from multidict import MultiDict

from jinja2 import Environment, PackageLoader
import json
import time
import png
from io import BytesIO
from aiohttp.abc import AbstractAccessLogger
from twisted.logger import Logger

from piqueserver.config import config, cast_duration

status_server_config = config.section("status_server")
host_option = status_server_config.option("host", "0.0.0.0")
port_option = status_server_config.option("port", 32886)
logging_option = status_server_config.option("logging", False)
interval_option = status_server_config.option(
    "update_interval", default="1min", cast=cast_duration)
scripts_option = config.option("scripts", [])


def as_future(d):
    return d.asFuture(asyncio.get_event_loop())


def as_deferred(f):
    return Deferred.fromFuture(asyncio.ensure_future(f))


class AccessLogger(AbstractAccessLogger):

    def log(self, request, response, time):
        self.logger.info(
            "{remote} {method} {url}: {status} {time:0.2f}ms -- {ua}",
            remote=request.remote,
            ua=request.headers["User-Agent"],
            method=request.method,
            url=request.url,
            time=time * 1000,
            status=response.status)


async def set_default_headers(request, response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Credentials'] = 'true'

def current_state(protocol):
    """Gathers data on current server/game state from protocol class"""
    players = []

    for player in protocol.players.values():
        player_data = {}
        player_data['name'] = player.name
        player_data['latency'] = player.latency
        player_data['client'] = player.client_string
        player_data['kills'] = player.kills
        player_data['team'] = player.team.name

        players.append(player_data)

    dictionary = {
        "serverIdentifier": protocol.identifier,
        "serverName": protocol.name,
        "serverVersion": protocol.version,
        "serverUptime": time.time() - protocol.start_time,
        "gameMode": protocol.game_mode_name,
        "map": {
            "name": protocol.map_info.name,
            "version": protocol.map_info.version,
            "author": protocol.map_info.author
        },
        "scripts": scripts_option.get(),
        "players": players,
        "maxPlayers": protocol.max_players,
        "scores": {
            "currentBlueScore": protocol.blue_team.score,
            "currentGreenScore": protocol.green_team.score,
            "maxScore": protocol.max_score}
    }

    return dictionary


class StatusServer(object):
    def __init__(self, protocol):
        self.protocol = protocol
        self.last_update = None
        self.last_map_name = None
        self.cached_overview = None
        env = Environment(loader=PackageLoader('piqueserver.web'))
        self.status_template = env.get_template('status.html')

    async def json(self, request):
        state = current_state(self.protocol)
        return web.json_response(state)

    @property
    def current_map(self):
        return self.protocol.map_info.name

    def update_cached_overview(self):
        """Updates cached overview"""
        overview = self.protocol.map.get_overview(rgba=True)
        w = png.Writer(512, 512, alpha=True)
        data = BytesIO()
        w.write_array(data, overview)
        self.cached_overview = data.getvalue()
        self.last_update = time.time()
        self.last_map_name = self.protocol.map_info.name

    async def overview(self, request):
        # update cache on a set interval or map change or initialization
        if (self.cached_overview is None or
                self.last_map_name != self.current_map or
                time.time() - self.last_update > interval_option.get()):
            self.update_cached_overview()

        return web.Response(body=self.cached_overview,
                            content_type='image/png')

    async def index(self, request):
        rendered = self.status_template.render(server=self.protocol)
        return web.Response(body=rendered, content_type='text/html')

    async def listen(self):
        """Starts the status server on configured host/port"""
        print("StatusServer")
        app = web.Application()
        app.on_response_prepare.append(set_default_headers)
        app.add_routes([
            web.get('/json', self.json),
            web.get('/overview', self.overview),
            web.get('/', self.index)
        ])
        logger = Logger() if logging_option.get() else None
        log_class = AccessLogger if logging_option.get() else None
        runner = web.AppRunner(app,
                               access_log=logger,
                               access_log_class=log_class)
        await as_deferred(runner.setup())
        site = web.TCPSite(runner, host_option.get(), port_option.get())
        await as_deferred(site.start())

        # TODO: explain why we do this
        await Deferred()
