"""Hub settings, read from environment variables (prefix ``TOPCOAT_``)."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOPCOAT_", env_file=".env", extra="ignore")

    # SQLAlchemy URL, psycopg 3 driver (used for both async app and sync Alembic).
    database_url: str = "postgresql+psycopg://topcoat:topcoat@localhost:5432/topcoat"

    # Dev-only authentication: a static bearer token that maps to the seeded dev admin.
    # Empty means "no GUI access at all". Replaced by OIDC in stage 5.
    dev_admin_token: str = ""

    # Directory holding the JSON Schemas of every protocol version (proto/v0, proto/v1, ...).
    proto_root: Path = REPO_ROOT / "proto"

    # Built web GUI (web/dist). Served under / when the directory exists.
    web_dist: Path = REPO_ROOT / "web" / "dist"

    # TLS (ARCHITECTURE D11, hub/src/topcoat_hub/tls.py): auto | files | off.
    tls_mode: Literal["auto", "files", "off"] = "auto"
    # auto: where the hub CA and server certificate live (a volume in the container).
    tls_dir: Path = REPO_ROOT / "hub" / ".tls"
    # where secrets.key lives (tokens of registered repositories are sealed with it); empty =
    # next to the hub CA in tls_dir, which is a volume that survives updates
    secret_dir: Path | None = None
    # auto: comma-separated DNS names / IPs the nodes use to reach the hub.
    tls_hostnames: str = ""
    # files: admin-provided certificate (chain) and key; optional CA for pairing commands.
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    tls_ca_file: Path | None = None
    # URL shown in pairing commands (e.g. the reverse proxy's https URL). Empty: the GUI
    # uses the address it was opened with.
    public_url: str = ""
    # Listen address of `python -m topcoat_hub.serve`.
    host: str = "0.0.0.0"  # noqa: S104 - the hub is meant to be reached from the LAN
    port: int = 8443

    # Signed update channel for the nodes (E2-06, D50). Empty update_dir switches the channel
    # off. The hub only distributes: nodes verify against the keys built into their own program.
    update_dir: Path | None = None
    # Optional: comma-separated base64 ed25519 public keys. With them the hub also refuses an
    # upload that no key signed (the same keys the nodes are built with).
    update_pubkeys: str = ""
    # How many releases per channel and platform are kept (the newest ones).
    update_keep: int = 2
    # Where the artifacts of the build service wait for the admin's signature (NS-01). Empty:
    # below `update_dir`, in `.builds`. Without either, the Releases page is switched off.
    release_dir: Path | None = None
    # The node on the production host that rolls the hub out (hub.update, NS-01 phase 5). Empty:
    # no roll-out from the page.
    rollout_node: str = ""
    # How long a build step may take (a node build compiles two platforms).
    release_step_timeout_seconds: float = 1800.0

    pairing_code_ttl_seconds: int = 15 * 60
    action_timeout_seconds: float = 30.0
    # on shutdown, fire-and-forget work (note extraction, a started run) may finish for this long;
    # what is left is cancelled and awaited before the database engine is disposed
    shutdown_grace_seconds: float = 3.0
    hello_timeout_seconds: float = 10.0
    # a direct model transfer (E2-07) that reports no progress for this long fails with "timeout"
    model_transfer_timeout_seconds: float = 15 * 60.0
    heartbeat_interval_seconds: int = 10
    sse_keepalive_seconds: float = 15.0

    # Model gateway (/v1, E1-05).
    # Which node and model do which work is decided by the table of applications of the node
    # policy (the GUI, model_resolver.py). The variables that used to steer it
    # (legacy_policy.LEGACY_ENV: TOPCOAT_CHAT_NODE_CHAIN, TOPCOAT_GATEWAY_NODE_ORDER,
    # TOPCOAT_GATEWAY_BACKGROUND_NODES, TOPCOAT_GATEWAY_BACKGROUND_MODEL, TOPCOAT_MEMORY_MODEL,
    # TOPCOAT_REVIEW_MODEL) are read once at start and turned into rows of the table
    # (legacy_policy.convert); they are not settings any more.
    # Node choice by time: how fast a model comes off the disk (MB/s, a rough default) and how
    # long a typical reply is (tokens); used only when every candidate has a measured speed.
    gateway_load_mb_per_second: float = 400.0
    # Hand-over of heavy chat requests to the task queue (only when the person asks for it).
    handoff_enabled: bool = True
    # The second look: a model other than the author reads answers of the last ``review_hours``.
    review_enabled: bool = True
    # a plain acknowledgement (thanks, ok) is answered with an emoji, not a text block
    reactions_enabled: bool = True
    # each answer gets its second look right after it is given (not only when the chat rests), and
    # what the look finds reaches the chat's next answer
    review_after_answer: bool = True
    # A second model reads the agent's draft BEFORE it goes out when the task used one of these
    # tools (comma separated; empty = off): slow (the reviewer is the slow node), for work where a
    # wrong answer costs most.
    review_before_answer_tools: str = "repo.test_start,repo.apply_patch"
    precheck_timeout_seconds: float = 240.0
    review_hours: int = 24
    review_catchup_minutes: int = 30  # scheduled look, when each answer is reviewed at once
    review_batch: int = 3
    # 0 = no clock: answers are reviewed when a chat is finished (``ChatDigester``). A value > 0
    # adds the old fixed-interval look on top.
    review_tick_seconds: float = 0.0
    # a model reads answers on its own (the scheduled look, also when a chat rests). Off since
    # 2026-11-14: the hub checks citations without a model, a model reads when the person asks
    review_scheduled: bool = False
    # The two looks at a finished chat: the author writes a summary, another model reads chat and
    # summary and learns what the author did not. Event-driven: a chat counts as finished when it
    # rested ``chat_digest_idle_seconds``; nothing runs on a clock.
    chat_digest_enabled: bool = True
    chat_digest_idle_seconds: float = 600.0
    chat_digest_min_messages: int = 4
    # the author updates the summary at once when it has answered (0), so that it is done before
    # the next question; a busy node is asked again after the retry spacing
    chat_digest_summary_delay_seconds: float = 0.0
    chat_digest_summary_retry_seconds: float = 15.0
    chat_digest_retries: int = 6  # tries when the nodes are busy (one rest period apart)
    chat_digest_max_learned: int = 3
    chat_digest_catchup_hours: int = 48  # after a restart: unfinished chats of this age are done
    chat_digest_catchup_max: int = 10
    # One continuous conversation (user area): the hub cuts it into segments by rests of
    # ``segment_idle_seconds`` (event-driven, no timer); a new segment starts from the hand-over
    # of the last closed one plus up to ``segment_recall_max`` older segments that fit the question.
    main_thread_enabled: bool = False
    segment_idle_seconds: float = 1200.0
    segment_recall_max: int = 2
    gateway_typical_reply_tokens: int = 300
    gateway_max_request_bytes: int = 1024 * 1024
    # Loading a model can take minutes (large GGUF from a slow disk).
    gateway_load_timeout_seconds: float = 600.0
    # a model unused for this long is idle: background work may unload it (max_loaded)
    gateway_keep_loaded_seconds: float = 120.0
    # a model is loaded only when its size plus this share of its size (percent) fits into the
    # node's free memory; otherwise idle models are unloaded (least recently used first) or the
    # load waits
    gateway_load_buffer_percent: float = 20.0
    # a node with less free RAM than this loads no model at all (idle models are unloaded first)
    gateway_min_free_ram_mb: float = 2048.0
    # First bytes of an answer: prompt processing of a long context runs before them.
    gateway_first_byte_timeout_seconds: float = 300.0
    # background work (summaries, care) may wait longer: a big model on a slow node reads a long
    # prompt for minutes, and nobody is waiting for the answer
    gateway_background_first_byte_timeout_seconds: float = 1200.0
    # Pause between two parts of a streamed answer before the hub gives up.
    gateway_idle_timeout_seconds: float = 120.0
    # how long an answer may stand still while its node says it is paused for local use (D24);
    # the node unloads its models after its own limit (config.yield) and ends the stream then
    gateway_pause_timeout_seconds: float = 3600.0
    # Tool nodes (E2-04): how long the hub waits for a capability call, and how much of a call's
    # output the audit log keeps (0 = none; the caller always gets the whole output).
    tool_timeout_seconds: float = 45.0
    tool_audit_output_bytes: int = 16 * 1024
    # Old rows leave the tables that only grow (retention.py): what the nodes reported, and the
    # audit log of capability calls. 0 days = keep for ever; the tick is how often the hub looks
    # (0 = never on its own).
    retention_node_events_days: int = 90
    retention_tool_calls_days: int = 365
    retention_tick_seconds: float = 6 * 3600.0
    # The inventory of what runs on the nodes (E3-10): how often each connected node is read
    # (0 = only on demand), how long a container or service that vanished stays listed as gone.
    # A look older than three intervals is shown as stale, never as current.
    inventory_interval_seconds: float = 300.0
    inventory_gone_days: int = 14
    # Topcoat as a tool of the assistant (read-only: guide, own chats, own settings, servers,
    # admin-only config). False switches it off for the whole hub; users also switch it per
    # conversation with the tool group "topcoat".
    self_tool_enabled: bool = True
    # The identity view (E3-12): how long a read of Keycloak is reused (memory only, never stored).
    identity_cache_seconds: float = 60.0
    # The agent (E3-01a): limits of one run of a task. A run ends when one is hit; the agent then
    # writes its diagnosis with what it has. "Continue" starts a new run with fresh limits.
    agent_enabled: bool = True
    # a node comes online: tasks that stopped for lack of one go on (resume_stuck.py)
    resume_stuck_enabled: bool = True
    resume_stuck_hours: float = 72.0  # how long a task that stopped for lack of a node waits
    resume_stuck_max: int = 5  # tasks continued per person and arrival
    # the regular re-check of pinned answers (recheck.py): an answer that was handed back is
    # asked for again, compared, and marked still correct or outdated
    recheck_enabled: bool = True
    # Gründlich: a small model on the checking node looks at each tool step while the run goes on
    # (step_check.py, the plan section 7); the chat's answer is still checked before it is shown
    step_check_enabled: bool = False  # off unless an admin switches it on (PRG-00 R5)
    step_check_model: str = "Qwen3-4B-Instruct-2507-Q4_K_M"
    # Gründlich: how often an answer may be revised after its check (0 = checked, not revised)
    effort_thorough_revisions: int = 2
    # a finished hand-over's file (the request, the report with its sources) goes into the
    # person's knowledge base, in the private source "Akten"
    handover_to_knowledge: bool = True
    recheck_interval_hours: float = 24.0
    recheck_max: int = 5  # pinned answers re-checked per round
    resume_stuck_attempts: int = 3  # a task is started this many times at most this way
    agent_max_turns: int = 30  # model calls per run
    agent_max_tool_calls: int = 20
    # Phase 1 (2026-11-11): a chat check is planned in steps, each with a fresh context
    agent_staged: bool = False
    agent_stage_calls: int = 6  # the tool calls of one working step
    agent_max_web_searches: int = 3  # web.search calls per run
    agent_max_web_fetches: int = 2  # web.fetch calls per run
    agent_time_budget_seconds: float = 600.0
    # A task of Projekte run in Entwicklung (dev: reading and changing code) is bigger than a chat
    # check (the maintainer, 2026-11-17: tasks grow, slower is fine). Live: a code survey stopped at
    # 20 calls with a prompt of 20000 characters on a 128k slot. The hub still fits the prompt to
    # the slot of the model that runs it (agent_loop._fit_to_context).
    task_max_tool_calls: int = 60
    task_max_turns: int = 90
    task_time_budget_seconds: float = 2700.0
    task_context_chars: int = 160000  # about 45k tokens of transcript
    task_observation_chars: int = 8000  # what the model sees of one file or search at once
    # The Research program (research_profile.py): a run there is not cut by a count or the clock.
    # It ends when its frame (quick, default, thorough) is satisfied, or when it makes no progress
    # (``research_stall_turns`` model turns in a row without a new source or a new piece of
    # evidence). 0 = no limit for the counts and times below; the stall guard is off at 0 too.
    research_max_turns: int = 0
    research_max_tool_calls: int = 0
    research_time_budget_seconds: float = 0.0
    research_stall_turns: int = 12
    research_quick_seconds: float = 900.0  # a quick look has a soft time frame, in minutes
    research_output_tokens: int = 4096  # one model answer; a long report is written in sections
    research_observation_chars: int = 8000  # what the model sees of one page or search result
    research_context_chars: int = 48000  # the whole prompt of a research run
    research_min_context: int = 24576  # the slot a research run needs (tokens)
    # the models can read images, so the agent is offered browser.shot (E6-02); the hub cannot tell
    browser_shot_allowed: bool = False
    agent_model_timeout_seconds: float = 300.0  # the coordinator's planning calls only
    # the Akte of the projects (tables project_events and project_node_versions) grows with every
    # change; above this size (MB, both tables together) the admin page shows a soft warning.
    # Nothing is deleted because of it; 0 = no warning
    project_akte_warn_mb: int = 500
    # the time zone of the installation (an IANA name); a person may choose their own
    # (users.timezone).
    # Instants are stored in UTC either way; this decides what "today" and "09:00" mean (D4-4)
    hub_timezone: str = "UTC"
    # The reminder tick of the projects (04h, background task "project_reminders", D75): how often
    # the hub looks for due reminders (0 = the tick does not run), and how late a reminder may be
    # and still be delivered after downtime or quiet hours (older ones are closed without a
    # message; the Akte says so)
    project_reminder_tick_seconds: float = 60.0
    project_reminder_catchup_hours: int = 48
    # the daily list of overdue tasks, for people who chose an hour for it
    project_digest_enabled: bool = True
    # An upper limit for one model answer of an agent run, 0 = none. A run is not cut off by
    # the clock: a slow node may take minutes, and every piece of the streamed answer shows that
    # it is alive. The run ends when the node is gone or stands still (gateway_idle_timeout).
    agent_model_hard_cap_seconds: float = 0.0
    # Ask the model for a streamed answer and put it together in the hub (see
    # Gateway.call_streamed); False = one request, one answer, no sign of life before the end.
    agent_stream: bool = True
    agent_model_retry_seconds: float = 10.0  # pause between tries when no node can serve the model
    agent_observation_chars: int = 3500  # what the model sees of one tool output
    agent_context_chars: int = 20000  # the whole prompt; older tool outputs are dropped first
    agent_max_active_per_user: int = 3
    # E3-01b: the agent may propose state-changing actions (restart a container or a unit) that run
    # only after the owner's click. Off by default: nothing changes until an admin switches it on
    # (and a node's config lists what may be restarted).
    proposals_enabled: bool = False
    # E3-08: the coordinator splits a question into read-only checks per machine. Few at a time:
    # a node has one model slot, so more parallel checks only queue for the model.
    coordinator_enabled: bool = True
    coordinator_parallel: int = 2
    coordinator_max_children: int = 6
    coordinator_time_budget_seconds: float = 900.0
    coordinator_child_turns: int = 12
    coordinator_child_tool_calls: int = 8
    coordinator_child_time_seconds: float = 300.0
    coordinator_poll_seconds: float = 1.0
    proposal_ttl_seconds: int = 1800  # a proposal not clicked by then expires
    proposals_max_per_task: int = 3
    # how the agent talks to a model: auto (native tool calls, JSON where the model cannot),
    # native or json; overridable per model in the GUI (hub setting agent_tool_modes)
    agent_tool_mode: str = "auto"
    # Context of ONE slot that an agent (and a chat that can use tools) asks a node to load a model
    # with when the node's own config gives less (or nothing, then 8192): tool results and the
    # diagnosis need room. 0 = do not ask. A model that is already loaded with less is only
    # reported (a note in the task), never reloaded.
    agent_min_context: int = 16384
    # the effort level (fast, balanced, thorough) whose entry of ``chat_effort`` the agent's own
    # model calls send, e.g. the switch that turns thinking off for gemma; no entry = nothing sent
    agent_effort: str = "fast"
    agent_tools_wait_seconds: float = 60.0  # after a restart the tool nodes reconnect first
    # a chat check is offered only the tool groups its question names (shorter tool definitions)
    agent_auto_tool_groups: bool = True
    # Wake-on-LAN (E2-05 E): the LAN's broadcast address the magic packet goes to, and the port
    wol_broadcast: str = "255.255.255.255"
    wol_port: int = 9
    # A request that waits in a node's queue counts one place better for every this many seconds
    # (0 = off), so background work cannot starve behind a steady stream of chat requests.
    gateway_queue_aging_seconds: float = 0.0
    # A queue stays short: when this many requests wait for a node (0 = no cap), or this many from
    # one person and source (background, agent, bench), a new one is refused at once with
    # ``queue_full`` instead of waiting for hours. A chat request is never refused by these.
    gateway_queue_max_waiting: int = 100
    gateway_queue_max_waiting_per_source: int = 20
    gateway_retry_after_seconds: int = 30

    # Chat (E2-01). The default model is the default of the pool (PRG-00 R2): the admin picks
    # it on the pools page (stored in hub_settings), else the best answering model of the pools.
    # Context length assumed for a model that is not loaded (the node's default for -c).
    chat_default_context_tokens: int = 8192

    # Memory (E2-02). Embeddings: empty = the transitional hashing embedder (no model needed);
    # set to an embedding model served through the gateway (bge-m3 after E2-03) to use it.
    embedding_model: str = ""
    # without embedding_model: use bge-m3 as soon as a node serves it (the transitional embedder
    # until then); false = always the transitional one
    embedding_auto: bool = True
    # how often that check looks for the model (seconds; 0 = no background check, tests)
    embedding_watch_seconds: float = 120.0
    # The idle consolidation looks at unchecked notes this often (0 = never on its own; the
    # GUI can still start a run) ...
    memory_consolidation_interval_seconds: float = 1800.0
    # D48: the assistant writes rules and styles it hears in a conversation (marked, undoable)
    memory_agent_learns: bool = True
    # Conflicting notes are settled by the hub (stronger evidence, then the latest word; undoable
    # from the history) instead of asking the person. False: the person gets a question again.
    memory_auto_settle: bool = True
    # ... and only when a node is `available`. 0: background work does not wait for a quiet
    # time -- the queue of the node serves chat first and takes background work as soon as
    # nothing more important is left. A value above 0 adds the old rule: no chat answer ran for
    # this long.
    memory_idle_seconds: float = 0.0
    # Budget of one run (model tokens and seconds).
    memory_run_max_tokens: int = 6000
    memory_run_max_seconds: float = 300.0
    # How long the chat waits after an answer for the "remembered: …" note.
    memory_note_wait_seconds: float = 8.0

    # Memory care (E3-05). Reliability = base x trust x 0.5^(age / half-life of the kind); every
    # confirmation resets the clock and lengthens the half-life (x boost per confirmation, at most
    # x boost_max); a live check counts `live_weight` confirmations.
    memory_halflife_person_days: float = 365.0
    memory_halflife_preference_days: float = 180.0
    memory_halflife_operational_days: float = 14.0
    # what Dev knows (a branch, how the tests run, a day of work) is stale after days, not a year
    memory_halflife_dev_days: float = 7.0
    memory_halflife_volatile_days: float = 1.0
    memory_halflife_boost: float = 1.5
    memory_halflife_boost_max: float = 8.0
    memory_live_check_weight: float = 2.0
    # a volatile state ("service X is down") is kept at most this long (0 = never stored)
    memory_volatile_ttl_hours: float = 24.0
    # E3-07 D4: sampling parameters sent with every chat request, by model family (a part of the
    # model id, case-insensitive; the longest match wins; `{}` switches a default off), e.g.
    # TOPCOAT_CHAT_SAMPLING='{"gemma": {"repeat_penalty": 1.15}, "qwen": {}}'
    chat_sampling: dict[str, dict[str, float]] = {}
    # Prototype: what each quality level sends to a model family to set how long it thinks, e.g.
    # TOPCOAT_CHAT_EFFORT='{"qwen": {"fast": {"reasoning_effort": "low"}}}' (empty = nothing sent).
    chat_effort: dict[str, dict[str, dict[str, Any]]] = {}
    # the project issues from the running system are filed in (project_issues.py), by its title
    issue_project: str = "Topcoat"
    # the directory the hub hands a fix to the host's roll-out (issue_fixes.py, D104); empty: none
    fix_spool_dir: str = ""
    # stages the model sees (never numbers): reliability >= sure: sure, >= older: older, else stale
    memory_stage_sure: float = 0.7
    memory_stage_older: float = 0.35
    # a note below this reliability is checked by the care run
    memory_check_threshold: float = 0.5
    # a note is archived only if usefulness AND reliability are below these AND it has not been
    # used for this many days (age alone never archives)
    memory_archive_usefulness: float = 0.3
    memory_archive_reliability: float = 0.25
    memory_archive_idle_days: float = 90.0
    # The care run: nightly at this hour (server time; -1 = no fixed hour), and when the hub has
    # been idle; the tick looks this often (0 = never on its own, the GUI can still start a run).
    memory_care_hour: int = 3
    memory_care_interval_seconds: float = 900.0
    # budget of one run, and the tool calls of the check of one note
    memory_care_max_notes: int = 10
    memory_care_run_max_seconds: float = 3600.0
    # one check of the care: the time of one model turn and of the whole task. A big model on a
    # slow node (the laptop) reads a prompt for minutes and nobody waits for the answer.
    memory_care_model_timeout_seconds: float = 900.0
    memory_care_task_seconds: float = 1800.0
    memory_care_task_calls: int = 4
    # a note is not checked again sooner than this many days
    memory_care_recheck_days: float = 3.0
    # idle care runs (besides the night) at most this often per user
    memory_care_idle_every_hours: float = 12.0

    # Knowledge base (E2-03): extra exclude globs (comma-separated) on top of the fixed list
    # (*SENSIBEL*, *ZUGANGSDATEN*, *.key, *.pem, *.env, *password*, *secret* …), and how often
    # every source is indexed again (0 = only when started in the GUI).
    knowledge_exclude: str = ""
    knowledge_reindex_seconds: float = 6 * 3600
    # a directory file (10_KI_Verzeichnis.md) older than this is reported as possibly out of date
    knowledge_directory_stale_days: int = 14
    # the most of a hit that is shown; a hit is the whole chunk (1200 characters at most), so a
    # larger value changes nothing, a smaller one shortens the hits
    knowledge_hit_chars: int = 1200
    # GitHub as a read-only tool of the assistant (admin only): the code, the pull requests and the
    # issues of the repositories registered on the Repositories page (each with its own
    # fine-grained token, read rights only, stored encrypted); there is no setting for a token.
    github_enabled: bool = True
    github_api_url: str = "https://api.github.com"
    github_verify_tls: bool = True

    # The core (E3-06): the block at the start of every system prompt (rules, style, profile,
    # household, system rules); a model context below 8k gets at most 500 tokens of it. The latest
    # chats ("discussed last": key terms of their running summaries) have their own budget on top.
    core_tokens: int = 1000
    core_recent_tokens: int = 150
    core_recent_chats: int = 2  # how many of the latest chats of the program the block names
    core_recent_days: int = 14  # a chat older than this is no longer "discussed last"

    # No data goes to an external model: the hub has no code that calls one (the escalation of
    # E3-02 was removed; see ARCHITECTURE D33). The redaction layer stays for what it still guards
    # (web search queries, hand-over summaries). Own public domains (comma-separated) that it
    # pseudonymises on top of the private top-level names (.lan, .local, ...).
    redact_domains: str = ""

    @field_validator("tls_cert_file", "tls_key_file", "tls_ca_file", "secret_dir", mode="before")
    @classmethod
    def _empty_path_is_none(cls, value: object) -> object:
        # Compose passes unset optional variables as "".
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
