"""Fulla's tools. Each module holds its dependencies at module level, set once by init()."""

from fulla import groupctx
from fulla.tools import calendar as calendar_mod
from fulla.tools import clock, facts, mac_queue, reminders
from fulla.tools import directives as directives_mod
from fulla.tools import feedback as feedback_mod
from fulla.tools import followups as followups_mod
from fulla.tools import gaps as gaps_mod
from fulla.tools import group_github as group_github_mod
from fulla.tools import group_linear as group_linear_mod
from fulla.tools import group_memory as group_memory_mod
from fulla.tools import group_reminders as group_reminders_mod
from fulla.tools import groups as groups_mod
from fulla.tools import imessage as imessage_mod
from fulla.tools import machines as machines_mod
from fulla.tools import messages as messages_mod
from fulla.tools import people as people_mod
from fulla.tools import runs as runs_mod
from fulla.tools import schedule as schedule_mod
from fulla.tools import soul as soul_mod
from fulla.tools import web as web_mod
from fulla.tools.calendar import calendar, connect_calendar, disconnect_calendar, list_calendars
from fulla.tools.clock import now
from fulla.tools.directives import drop_directive, list_directives, set_directive
from fulla.tools.facts import forget, recall, remember
from fulla.tools.feedback import (
    list_feedback,
    note_feedback,
    note_feedback_shipped,
    resolve_feedback,
)
from fulla.tools.followups import (
    deliver_followup,
    drop_followup,
    list_followups,
    schedule_followup,
    watch_environment,
)
from fulla.tools.gaps import list_gaps, note_gap, note_gap_shipped, propose_gap_fix, resolve_gap
from fulla.tools.group_github import github_tools
from fulla.tools.group_info import group_sources
from fulla.tools.group_linear import linear_edit_tools, linear_tools
from fulla.tools.group_memory import group_recall, group_remember
from fulla.tools.group_reminders import (
    drop_group_reminder,
    schedule_group_followup,
    schedule_group_reminder,
)
from fulla.tools.groups import (
    connect_group_source,
    disconnect_group_source,
    list_groups,
    set_group_credential,
    set_group_tools,
)
from fulla.tools.imessage import draft_imessage
from fulla.tools.mac_queue import drop_mac_task, list_mac_queue, queue_for_mac
from fulla.tools.machines import describe_machine, forget_machine, list_machines
from fulla.tools.messages import send_message
from fulla.tools.people import allow_group, allow_person, drop_group, drop_person, list_people
from fulla.tools.reminders import drop_reminder, list_reminders, schedule_reminder
from fulla.tools.runs import norns_run
from fulla.tools.schedule import drop_trigger, list_triggers, set_trigger
from fulla.tools.soul import show_soul, write_soul
from fulla.tools.web import fetch_url, web_search

manager_tools = [
    now, calendar, connect_calendar, disconnect_calendar, list_calendars,
    remember, recall, forget, schedule_reminder, list_reminders, drop_reminder,
    set_directive, drop_directive, list_directives,
    show_soul, write_soul,
    note_feedback, list_feedback, note_feedback_shipped, resolve_feedback,
    note_gap, list_gaps, propose_gap_fix, note_gap_shipped, resolve_gap,
    queue_for_mac, list_mac_queue, drop_mac_task,
    draft_imessage,
]
# A message to the owner mid-turn, delivered as the final reply is (channel.say).
# Owner-only by being the manager's, and by the run (runctx.current_is_manager).
manager_tools += [send_message]
# Fulla's own check-backs.
manager_tools += [schedule_followup, list_followups, drop_followup, watch_environment]
# Her timers in Norns, the scheduled turns among them. Owner-only by being the manager's.
manager_tools += [list_triggers, set_trigger, drop_trigger]
# One run by its id, for "what happened in run 938". Owner-only the same way.
manager_tools += [norns_run]
# Who else may talk to her, and where. Owner-only by being the manager's.
manager_tools += [allow_person, drop_person, list_people, allow_group, drop_group]
# The owner's machines: what each is for, and forgetting one. Owner-only the same way.
manager_tools += [list_machines, describe_machine, forget_machine]
# What each allowed group has: tools, sources, credentials. Owner-only the same way.
manager_tools += [list_groups, set_group_tools, connect_group_source, disconnect_group_source, set_group_credential]
# The same GitHub and Linear tools the groups get, over the owner's own
# sources (group "me"), by the same rule: the scope comes from the run. The
# owner's own Linear is theirs to edit, so the edit tools are always here.
manager_tools += list(github_tools) + list(linear_tools) + list(linear_edit_tools)
followup_tools = [now, deliver_followup]
# The sandbox watcher: the same delivery, plus Bubbo's two read tools,
# allowlisted by name in `worker.build_agents` and held read-only by
# `coding.CodingGuard`.
watch_tools = [now, deliver_followup]
reminder_tools = [now]
mac_task_tools = [now]
# The web is read by its own agent, never the manager: see web.py.
reader_tools = [now, web_search, fetch_url]
# Friends and groups get the clock, the reader (through launch_agent) and
# reminders that post back into their own chat, and nothing that touches
# the owner's life. Adding a tool here is a decision about what strangers
# can reach.
social_tools = [now, schedule_group_reminder, drop_group_reminder, group_sources]
# What each entry of the owner's catalogue (groups.CATALOGUE) unlocks for a
# group's agent, on top of `social_tools`. "web" is the reader, reached as a
# sub-agent, so it adds no tool here. Every tool here takes its group from
# the run and refuses outside that group's sources (groupctx). The GitHub
# writes and a Linear filing are cards for the owner; "linear_edit" is the
# owner's switch for a group to change its own issues at once (assign, state,
# priority, labels, comments, new issues; never a delete), with its own key
# and within its own teams and projects, and implies "linear". "coding" is
# Bubbo's sandbox tools, served by the Bubbo worker and so allowlisted by
# name in `worker.group_agent` (scoped by `coding.CodingGuard`, not here),
# plus the check-back the group's agent polls a job with.
GROUP_TOOL_SETS: dict[str, list] = {
    "web": [],
    "memory": [group_remember, group_recall],
    "github": list(github_tools),
    "linear": list(linear_tools),
    "linear_edit": list(linear_edit_tools),
    "coding": [schedule_group_followup],
}
group_reminder_tools = [now]
all_tools = manager_tools


def group_tools(enabled: list[str]) -> list:
    """The tools a group's agent holds: the social set plus what the owner
    enabled, each once. The allowlist is built from this, nothing else."""
    tools = list(social_tools)
    for name in enabled:
        for t in GROUP_TOOL_SETS.get(name, []):
            if t.name not in {x.name for x in tools}:
                tools.append(t)
    return tools


def init(*, client, store, calendars, directives, feedback, gaps, soul, push_prompt, config,
         people=None, propose=None, imessage_allow=dict, groups=None, push_groups=None, api=None,
         hosts=None, say=None):
    clock.init(timezone=config.fulla.timezone)
    messages_mod.init(say=say)
    if hosts is not None:
        machines_mod.init(store=hosts, push_prompt=push_prompt)
    calendar_mod.init(calendars=calendars)
    facts.init(store=store)
    reminders.init(client=client, api=api, data_dir=config.fulla.data_dir, timezone=config.fulla.timezone)
    mac_queue.init(client=client, data_dir=config.fulla.data_dir)
    directives_mod.init(store=directives, push_prompt=push_prompt)
    feedback_mod.init(store=feedback)
    gaps_mod.init(store=gaps)
    soul_mod.init(soul=soul, push_prompt=push_prompt)
    runs_mod.init(client=client, timezone=config.fulla.timezone)
    followups_mod.init(client=client, data_dir=config.fulla.data_dir, timezone=config.fulla.timezone,
                       conversation_key=config.conversation_key)
    web_mod.init(brave_api_key=config.fulla.brave_api_key)
    group_reminders_mod.init(client=client, data_dir=config.fulla.data_dir, timezone=config.fulla.timezone,
                             groups=groups, people=people)
    groupctx.init(store=groups, owner_name=config.fulla.owner_name)
    group_memory_mod.init(store=groups)
    group_github_mod.init(store=groups, propose=propose)
    group_linear_mod.init(store=groups, propose=propose)
    if people is not None:
        people_mod.init(store=people, on_group_allowed=push_groups)
    if people is not None and groups is not None:
        groups_mod.init(store=groups, people=people, push=push_groups, owner_name=config.fulla.owner_name)
    imessage_mod.init(propose=propose, allow=imessage_allow)  # pyright: ignore[reportArgumentType]
    schedule_mod.init(api=api, conversation_key=config.conversation_key, tz=config.fulla.timezone)
