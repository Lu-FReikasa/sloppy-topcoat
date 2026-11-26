"""FastAPI application factory. Run with ``uvicorn topcoat_hub.main:app``."""

import asyncio
import functools
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from topcoat_hub import (
    __version__,
    agent_contract,
    hub_log,
    node_rename,
    output_schemas,
    plugin_registry,
    project_events,
    tls,
    web_access,
)
from topcoat_hub.agent_loop import Limits
from topcoat_hub.agent_precheck import PreAnswerCheck
from topcoat_hub.agent_service import AgentService, GatewayModel, ToolsAdapter
from topcoat_hub.api import agent as agent_api
from topcoat_hub.api import artifacts as artifacts_api
from topcoat_hub.api import bench as bench_api
from topcoat_hub.api import blocks as blocks_api
from topcoat_hub.api import chat, core, dev_repos, gui, keys, memory, node, v1
from topcoat_hub.api import handovers as handovers_api
from topcoat_hub.api import identity as identity_api
from topcoat_hub.api import inbox as inbox_api
from topcoat_hub.api import incognito as incognito_api
from topcoat_hub.api import inventory as inventory_api
from topcoat_hub.api import knowledge as knowledge_api
from topcoat_hub.api import machines as machines_api
from topcoat_hub.api import mandates as mandates_api
from topcoat_hub.api import messages as messages_api
from topcoat_hub.api import metrics as metrics_api
from topcoat_hub.api import model_transfers as model_transfers_api
from topcoat_hub.api import params as params_api
from topcoat_hub.api import personas as personas_api
from topcoat_hub.api import plugins as plugins_api
from topcoat_hub.api import project as project_api
from topcoat_hub.api import project_export as project_export_api
from topcoat_hub.api import releases as releases_api
from topcoat_hub.api import reports as reports_api
from topcoat_hub.api import research as research_api
from topcoat_hub.api import reviews as reviews_api
from topcoat_hub.api import scheduling as scheduling_api
from topcoat_hub.api import sheets as sheets_api
from topcoat_hub.api import theme_workshop as theme_workshop_api
from topcoat_hub.api import tools as tools_api
from topcoat_hub.api import ui as ui_api
from topcoat_hub.api import updates as updates_api
from topcoat_hub.api import wiki as wiki_api
from topcoat_hub.bench.service import BenchService
from topcoat_hub.blocks.service import BlockService
from topcoat_hub.chat_digest import ChatDigester
from topcoat_hub.chat_review import ChatReviewer
from topcoat_hub.chat_runs import AnswerRuns, sweep_interrupted
from topcoat_hub.config import Settings, get_settings
from topcoat_hub.coordinator import Coordinator
from topcoat_hub.core import CoreService
from topcoat_hub.core_mirror import Mirror
from topcoat_hub.db import make_engine, make_sessionmaker
from topcoat_hub.devrepos import DevRepoStore
from topcoat_hub.embedding import AutoEmbedder, make_embedder
from topcoat_hub.gateway import Gateway, drain_background, spawn
from topcoat_hub.github_access import GitHubService
from topcoat_hub.handover import HandoverService
from topcoat_hub.identity import IdentityService
from topcoat_hub.incognito import IncognitoService
from topcoat_hub.inventory import InventoryService
from topcoat_hub.issue_fixes import FixService
from topcoat_hub.job_log import JobLog
from topcoat_hub.knowledge import KnowledgeService
from topcoat_hub.live import EventBus, NodeSessions
from topcoat_hub.live_settings import LiveSettings
from topcoat_hub.memory import MemoryService
from topcoat_hub.memory_care_service import MemoryCare
from topcoat_hub.memory_store import sweep_orphaned_runs
from topcoat_hub.message_service import MessageService
from topcoat_hub.message_tools import MessageTools
from topcoat_hub.metrics import NodeMetrics
from topcoat_hub.model_transfers import TransferCoordinator
from topcoat_hub.nodeauth import ChallengeStore
from topcoat_hub.project_check import SecondLook
from topcoat_hub.project_events import SYSTEM
from topcoat_hub.project_knowledge import ProjectReportIndex
from topcoat_hub.project_revise import Reviser
from topcoat_hub.project_runs import RunService, batch_places
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tick import ReminderTick
from topcoat_hub.project_tools import ProjectTools
from topcoat_hub.proposals import ProposalService
from topcoat_hub.protocol import PROTOCOL_VERSION, load_schema_sets
from topcoat_hub.queue_watch import QueueWatch
from topcoat_hub.releases import ReleaseService
from topcoat_hub.report_knowledge import ReportIndex
from topcoat_hub.resume_stuck import StuckTaskResumer
from topcoat_hub.retention import Retention
from topcoat_hub.scheduling_log import SchedulingLog
from topcoat_hub.secretbox import SecretBox
from topcoat_hub.segments import SegmentService
from topcoat_hub.service import NodeService
from topcoat_hub.sheets.service import SheetResearchService
from topcoat_hub.steering import SteeringBoard
from topcoat_hub.tools import ToolService
from topcoat_hub.topcoat_self import TopcoatService
from topcoat_hub.trace_tool import TraceService
from topcoat_hub.ui.runtime import build_runtime
from topcoat_hub.ui.seeds import ensure_builtin as ensure_ui_builtin
from topcoat_hub.updates import UpdateStore, parse_keys
from topcoat_hub.wake import UdpWakeSender, demand_waker
from topcoat_hub.wiki_tools import WikiTools

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        hub_log.install()  # recent warnings for issues reported from the running hub
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.schema_sets = load_schema_sets(settings.proto_root)
        output_schemas.set_proto_root(settings.proto_root)  # the answer forms of blocks (1.12)
        agent_contract.set_proto_root(settings.proto_root)  # the employees (1.9)
        app.state.schemas = app.state.schema_sets[PROTOCOL_VERSION]  # current version
        app.state.bus = EventBus()
        bus = app.state.bus
        # every committed change to a project reaches the person's open pages (project_events.py)
        project_events.on_change = lambda user, project: bus.publish(
            "project_changed", {"project_id": project, "_user": user}
        )
        app.state.sessions = NodeSessions()
        app.state.challenges = ChallengeStore()
        app.state.updates = UpdateStore(
            settings.update_dir, parse_keys(settings.update_pubkeys), settings.update_keep
        )
        release_root = settings.release_dir or (
            settings.update_dir / ".builds" if settings.update_dir else None
        )
        app.state.releases_root = release_root
        app.state.tls = tls.prepare(settings)
        if not app.state.tls.enabled:
            log.warning(
                "TOPCOAT_TLS_MODE=off: plain HTTP. Only run it like this behind a reverse proxy "
                "that terminates TLS, or in tests"
            )
        app.state.metrics = NodeMetrics()
        app.state.nodes = NodeService(app.state.sessionmaker, app.state.bus, app.state.sessions)
        app.state.nodes.updates = app.state.updates
        app.state.transfers = TransferCoordinator(
            app.state.sessionmaker,
            app.state.sessions,
            app.state.bus,
            action_timeout_seconds=settings.action_timeout_seconds,
            timeout_seconds=settings.model_transfer_timeout_seconds,
        )
        app.state.nodes.transfers = app.state.transfers
        app.state.scheduling_log = SchedulingLog()
        app.state.gateway = Gateway(app.state.sessionmaker, app.state.sessions, settings)
        app.state.gateway.job_log = JobLog(app.state.sessionmaker)
        await app.state.gateway.load_policy()
        app.state.tools = ToolService(
            app.state.sessionmaker,
            app.state.sessions,
            settings,
            # nothing private goes into a web search or URL, whoever makes the call
            web_access.make_guard(web_access.private_names_of(app.state.sessionmaker, settings)),
        )
        # the research profile "sheets": its web goes through a node like any other call
        app.state.sheet_research = SheetResearchService(
            app.state.sessionmaker, app.state.tools, app.state.gateway
        )
        app.state.releases = ReleaseService(
            app.state.sessionmaker,
            app.state.tools,
            app.state.updates,
            app.state.releases_root,
            settings.release_step_timeout_seconds,
            settings.rollout_node,
        )
        app.state.wake = UdpWakeSender()
        # a request for the house model of a sleeping node wakes it (plan step 1.7); the sender is
        # looked up on each wake, so a test can swap it
        app.state.gateway.waker = demand_waker(
            app.state.sessionmaker, app.state.nodes, _LateSender(app), settings
        )
        app.state.embedder = make_embedder(
            app.state.gateway, settings.embedding_model, settings.embedding_auto
        )
        app.state.memory = MemoryService(
            app.state.sessionmaker, app.state.gateway, settings, app.state.embedder
        )
        embedder_watch = None
        if isinstance(app.state.embedder, AutoEmbedder):
            log.warning(
                "memory: transitional hashing embedder (no embedding model configured); it "
                "becomes %s by itself once a node serves it, or set TOPCOAT_EMBEDDING_MODEL",
                app.state.embedder.preferred,
            )
            app.state.embedder.on_switch = lambda _name: app.state.knowledge.reindex_all()
            if settings.embedding_watch_seconds > 0:
                embedder_watch = asyncio.create_task(
                    app.state.embedder.watch(settings.embedding_watch_seconds)
                )
        elif app.state.embedder.id.startswith("hashing"):
            log.warning(
                "memory: transitional hashing embedder (TOPCOAT_EMBEDDING_AUTO is off); set "
                "TOPCOAT_EMBEDDING_MODEL once an embedding model is served through the gateway"
            )
        app.state.knowledge = KnowledgeService(
            app.state.sessionmaker, app.state.tools, app.state.embedder, settings
        )
        # what runs on which node, read on a timer and kept with its age (E3-10)
        app.state.inventory = InventoryService(
            app.state.sessionmaker,
            app.state.tools,
            app.state.sessions,
            settings,
            app.state.bus.publish,
        )
        # who is in which group and which group opens which app, read from Keycloak (E3-12)
        app.state.identity = IdentityService(app.state.sessionmaker, app.state.tools, settings)
        # Topcoat as a read-only tool of the assistant: guide, own chats and settings, servers
        app.state.topcoat_self = TopcoatService(app.state.sessionmaker, app.state.tools, settings)
        # GitHub as a read-only tool (admin only, needs a token and a repository)
        secret_box = SecretBox(settings.secret_dir or settings.tls_dir)
        app.state.devrepos = DevRepoStore(app.state.sessionmaker, secret_box, settings)
        # how an answer was handled + the file reader's `repo.show` (development program only)
        app.state.trace = TraceService(app.state.sessionmaker, secret_box, app.state.devrepos)
        app.state.topcoat_self.trace = app.state.trace
        # the dev workshop gets the repository of a repo.* call from here
        app.state.tools.repo_spec = app.state.devrepos.call_spec
        # the person's own steps on local repositories run through the tool service (D76)
        app.state.devrepos.tools = app.state.tools
        app.state.github = GitHubService(settings, app.state.devrepos)
        # the project agent's tools: its own rows only (PPH-01b)
        app.state.project_tools = ProjectTools(app.state.sessionmaker, settings.hub_timezone)
        app.state.message_tools = MessageTools(app.state.sessionmaker, settings.hub_timezone)
        app.state.agents = AgentService(
            app.state.sessionmaker,
            GatewayModel(app.state.gateway, app.state.sessionmaker, settings),
            ToolsAdapter(
                app.state.tools,
                app.state.knowledge,
                app.state.inventory,
                app.state.identity,
                app.state.topcoat_self,
                app.state.github,
                project=app.state.project_tools,
                messages=app.state.message_tools,
                browser_shot=settings.browser_shot_allowed,
                wiki=WikiTools(app.state.sessionmaker),
            ),
            app.state.bus.publish,
            settings,
        )
        app.state.project_tools.agents = app.state.agents  # sent requests join the queue
        app.state.message_tools.agents = app.state.agents

        # a project task's run (project_runs.py) starts and ends through the agents of the hub
        app.state.project_runs = RunService(app.state.sessionmaker, app.state.agents, SYSTEM)
        app.state.project_tools.runs = app.state.project_runs  # the person's click starts a batch
        # fixes of issues (issue_fixes.py, D103/D104): drafts from runs, applied and rolled out
        app.state.fixes = FixService(app.state.sessionmaker, app.state.tools, settings)
        app.state.project_runs.fixes = app.state.fixes
        app.state.fixes.runs = app.state.project_runs
        app.state.fixes.bus = app.state.bus  # "fix blocked" reaches the inbox at once

        async def _answer_requests(user_id: uuid.UUID, task_id: uuid.UUID, event: str) -> None:
            store = ProjectStore(app.state.sessionmaker, SYSTEM, settings.hub_timezone)
            await MessageService(store, app.state.agents, app.state.proposals).on_task_event(
                user_id, task_id, event
            )
            # a finished project task checks its result and starts the next task of its batch
            await app.state.project_runs.on_task_event(user_id, task_id, event)

        app.state.agents.loop.lifecycle = _answer_requests  # a finished task answers its request
        # the changes the agent may propose and the user may confirm (E3-01b); off by default
        app.state.proposals = ProposalService(
            app.state.sessionmaker, app.state.tools, settings, app.state.bus.publish
        )
        app.state.agents.loop.proposals = app.state.proposals
        app.state.message_tools.proposals = app.state.proposals  # a push under a mandate
        # notes and pauses the person sends into a running check (E7-01)
        app.state.steering = SteeringBoard()
        app.state.agents.loop.steering = app.state.steering
        # a second model reads the draft before it goes out, for work where a mistake costs most
        app.state.agents.loop.precheck = PreAnswerCheck(app.state.gateway, settings)
        # a result of a project task is confirmed by another node before review (project_check.py)
        app.state.project_runs.second_look = SecondLook(app.state.gateway, settings)
        # a row it does not confirm goes back to an author model with the reason (project_revise)
        app.state.project_runs.reviser = Reviser(app.state.gateway)

        # a batch runs as many tasks at once as the nodes of their applications have slots
        async def _batch_places(targets: set[str]) -> int:
            gw = app.state.gateway
            return batch_places(await gw.node_views(), gw.policy, targets)

        app.state.project_runs.places = _batch_places
        app.state.project_runs.policy = lambda: app.state.gateway.policy
        app.state.proposals.repos = app.state.devrepos
        # one question, one read-only check per machine, one merged report (E3-08)
        app.state.agents.coordinator = Coordinator(
            app.state.sessionmaker, app.state.agents, settings
        )
        # parameters an admin changes while the hub runs (E7-03): the shared Settings object is
        # changed in place; what was copied into an object at start gets a hook
        app.state.live = LiveSettings(settings, app.state.sessionmaker)
        app.state.live.hooks["agent"] = lambda s: setattr(
            app.state.agents.loop, "limits", Limits.of(s)
        )
        applied = await app.state.live.load()
        if applied:
            log.info("live settings: %d value(s) from the database applied", applied)
        app.state.incognito = IncognitoService(
            app.state.gateway, app.state.tools, app.state.sessionmaker, settings
        )
        app.state.blocks = BlockService(
            app.state.gateway, app.state.agents, app.state.sessionmaker, settings
        )
        # a project task may name a program: its check is that program (project_programs.py)
        app.state.project_runs.blocks = app.state.blocks
        app.state.bench = BenchService(app.state.gateway, app.state.sessionmaker, settings)
        app.state.core = CoreService(app.state.sessionmaker, settings)
        app.state.ui = build_runtime(settings.proto_root)
        # answers are generated by tasks of the hub, not by the requests that asked (E3-07)
        app.state.runs = AnswerRuns(app.state.sessionmaker)
        app.state.knowledge.mirror = Mirror(app.state.sessionmaker, app.state.memory)
        app.state.reviewer = ChatReviewer(
            app.state.sessionmaker, app.state.gateway, app.state.memory, settings
        )
        app.state.reviewer.speak = functools.partial(chat.speak_up, app)
        # a freed node place starts the chats the digester waits with (chat_digest.py: no timer)
        app.state.gateway.free_listeners.append(lambda: app.state.digester.kick())
        app.state.gateway.free_listeners.append(lambda: spawn(app.state.project_runs.kick_all()))
        # a node that connects after a restart can take the chats that waited for it
        app.state.sessions.on_connect.append(lambda: app.state.digester.kick())
        # batch runs: a node that joins, or a place that frees, takes the next task of the queue
        app.state.sessions.on_connect.append(lambda: spawn(app.state.project_runs.kick_all()))
        # after a roll-out restarted the hub: the outcome waits in the spool, tests are followed on
        app.state.sessions.on_connect.append(lambda: spawn(app.state.fixes.sync_rollouts()))
        spawn(app.state.fixes.resume())
        app.state.digester = ChatDigester(
            app.state.sessionmaker,
            app.state.gateway,
            app.state.memory,
            app.state.reviewer,
            settings,
        )
        app.state.segments = SegmentService(app.state.sessionmaker, settings)
        app.state.memory_care = MemoryCare(
            app.state.sessionmaker,
            app.state.gateway,
            app.state.memory,
            settings,
            digester=app.state.digester,
        )
        scheduler = None
        if settings.memory_consolidation_interval_seconds > 0:
            scheduler = asyncio.create_task(app.state.memory.scheduler())
        review_scheduler = None
        if settings.review_enabled and settings.review_tick_seconds > 0:
            review_scheduler = asyncio.create_task(app.state.reviewer.scheduler())
        care_scheduler = None
        if settings.memory_care_interval_seconds > 0:
            care_scheduler = asyncio.create_task(app.state.memory_care.scheduler())
        inventory_scheduler = None
        if settings.inventory_interval_seconds > 0:
            inventory_scheduler = asyncio.create_task(app.state.inventory.scheduler())
        retention_scheduler = None
        if settings.retention_tick_seconds > 0:
            app.state.retention = Retention(app.state.sessionmaker, settings)
            retention_scheduler = asyncio.create_task(app.state.retention.scheduler())
        stuck_resumer = None
        if settings.agent_enabled and settings.resume_stuck_enabled:
            app.state.stuck = StuckTaskResumer(
                app.state.bus, app.state.sessionmaker, app.state.agents, settings
            )
            app.state.stuck.start()
            stuck_resumer = app.state.stuck
        handovers = None
        if settings.agent_enabled:
            app.state.handovers = HandoverService(
                app.state.gateway,
                app.state.agents,
                app.state.sessionmaker,
                settings,
                app.state.bus,
                app.state.incognito.store,
            )
            app.state.handovers.knowledge = app.state.knowledge
            app.state.handovers.start()
            handovers = app.state.handovers
        project_tick_scheduler = None
        if settings.project_reminder_tick_seconds > 0:
            app.state.project_tick = ReminderTick(app.state.sessionmaker, settings)
            project_tick_scheduler = asyncio.create_task(app.state.project_tick.scheduler())
        # the place of a person's waiting tasks in the queue, on the stream (queue_watch.py)
        app.state.queue_watch = QueueWatch(
            app.state.gateway.pool_waiting_jobs, app.state.sessionmaker, app.state.bus.publish
        )
        queue_watch_task = asyncio.create_task(app.state.queue_watch.scheduler())
        project_index = ProjectReportIndex(app.state.sessionmaker, app.state.knowledge)
        app.state.report_index = ReportIndex(app.state.sessionmaker, app.state.knowledge)

        async def sync_documents() -> int:
            """The reports of the projects and the released research reports, into the person's
            private sources (one step of the background task ``knowledge``)."""
            return await project_index.sync_all() + await app.state.report_index.sync_all()

        app.state.knowledge.project_reports = sync_documents
        knowledge_scheduler = None
        if settings.knowledge_reindex_seconds > 0:
            knowledge_scheduler = asyncio.create_task(app.state.knowledge.scheduler())
        if not settings.dev_admin_token:
            log.warning("TOPCOAT_DEV_ADMIN_TOKEN is empty: the GUI API rejects every request")
        await app.state.nodes.mark_all_offline()
        swept = await app.state.transfers.sweep_interrupted()
        if swept:
            log.warning("model transfers: %d were running when the hub went down: failed", swept)
        orphaned = await sweep_orphaned_runs(app.state.sessionmaker)
        if orphaned:
            log.warning(
                "memory: %d run(s) were running when the hub went down: interrupted", orphaned
            )
        interrupted = await sweep_interrupted(app.state.sessionmaker)
        if interrupted:
            log.warning(
                "chat: %d answer(s) were running when the hub went down: interrupted", interrupted
            )
        await app.state.core.ensure_actors()
        written = await ensure_ui_builtin(app.state.sessionmaker)
        if written:
            log.info("ui: %d built-in workspace(s) or profile(s) written", written)
        # the built-in programs as plug-ins of the registry (D105), after their rows are written
        if registered := await plugin_registry.ensure_builtin(app.state.sessionmaker):
            log.info("plug-ins: %d built-in program(s) registered or refreshed", registered)
        # earlier names of renamed nodes still reach them (node_rename.py)
        if aliases := await node_rename.load_aliases(app.state.sessionmaker):
            log.info("nodes: %d earlier name(s) lead to renamed nodes", aliases)
        try:
            await app.state.digester.catch_up()
        except Exception:
            log.exception("chat digest: catching up failed")
        if settings.agent_enabled:
            resumed = await app.state.agents.recover()
            if resumed:
                log.info("agent: continuing %d task(s) a restart interrupted", resumed)
        try:
            yield
        finally:
            await app.state.releases.shutdown()
            await app.state.transfers.shutdown()
            app.state.digester.stop()
            await app.state.blocks.shutdown()
            await app.state.bench.shutdown()
            await app.state.agents.shutdown()
            await app.state.knowledge.shutdown()
            if embedder_watch is not None:
                embedder_watch.cancel()
                await asyncio.gather(embedder_watch, return_exceptions=True)
            if inventory_scheduler is not None:
                inventory_scheduler.cancel()
                await asyncio.gather(inventory_scheduler, return_exceptions=True)
            if retention_scheduler is not None:
                retention_scheduler.cancel()
                await asyncio.gather(retention_scheduler, return_exceptions=True)
            if stuck_resumer is not None:
                await stuck_resumer.stop()
            if handovers is not None:
                await handovers.stop()
            queue_watch_task.cancel()
            await asyncio.gather(queue_watch_task, return_exceptions=True)
            if project_tick_scheduler is not None:
                project_tick_scheduler.cancel()
                await asyncio.gather(project_tick_scheduler, return_exceptions=True)
            if knowledge_scheduler is not None:
                knowledge_scheduler.cancel()
                await asyncio.gather(knowledge_scheduler, return_exceptions=True)
            if scheduler is not None:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
            if care_scheduler is not None:
                care_scheduler.cancel()
                await asyncio.gather(care_scheduler, return_exceptions=True)
            if review_scheduler is not None:
                review_scheduler.cancel()
                await asyncio.gather(review_scheduler, return_exceptions=True)
            # fire-and-forget work (note extraction, a started consolidation or care run, a
            # stopped answer being stored) ends before the engine goes: cancel + await, never drop
            ended = await app.state.runs.shutdown(settings.shutdown_grace_seconds)
            if ended:
                log.info("chat: %d running answer(s) ended as interrupted", ended)
            cancelled = await drain_background(settings.shutdown_grace_seconds)
            if cancelled:
                log.info(
                    "shutdown: %d background task(s) were still running and were cancelled",
                    cancelled,
                )
            await engine.dispose()

    app = FastAPI(title="sloppy-topcoat hub", version=__version__, lifespan=lifespan)
    app.include_router(node.router)
    app.include_router(updates_api.router)
    app.include_router(releases_api.router)
    app.include_router(model_transfers_api.router)
    app.include_router(metrics_api.router)  # before gui: /nodes/metrics is not a node id
    app.include_router(machines_api.router)
    app.include_router(gui.router)
    app.include_router(scheduling_api.router)
    app.include_router(params_api.router)
    app.include_router(blocks_api.router)
    app.include_router(bench_api.router)
    app.include_router(inbox_api.router)
    app.include_router(project_api.router)
    app.include_router(messages_api.router)
    app.include_router(mandates_api.router)
    app.include_router(project_export_api.router)
    app.include_router(incognito_api.router)
    app.include_router(handovers_api.router)
    app.include_router(ui_api.router)
    app.include_router(plugins_api.router)
    app.include_router(theme_workshop_api.router)
    app.include_router(keys.router)
    app.include_router(dev_repos.router)
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(core.router)
    app.include_router(personas_api.router)
    app.include_router(tools_api.router)
    app.include_router(inventory_api.router)
    app.include_router(identity_api.router)
    app.include_router(agent_api.router)
    app.include_router(artifacts_api.router)
    app.include_router(reports_api.router)
    app.include_router(research_api.router)
    app.include_router(sheets_api.router)
    app.include_router(wiki_api.router)
    app.include_router(wiki_api.project_router)
    app.include_router(reviews_api.router)
    app.include_router(knowledge_api.router)
    app.include_router(v1.router)
    if settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    return app


def app() -> FastAPI:
    """Factory for ``uvicorn --factory topcoat_hub.main:app``."""
    logging.basicConfig(level=logging.INFO)
    return create_app()


class _LateSender:
    """Forwards to ``app.state.wake`` at send time (tests replace it with a fake)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def send(self, mac: str, broadcast: str, port: int) -> None:
        await self._app.state.wake.send(mac, broadcast, port)
