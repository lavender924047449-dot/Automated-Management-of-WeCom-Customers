"""Broadcast 模块入口。"""

from app.broadcast.account import (
    data_root_for,
    get_current_account_id,
    load_last_active_account,
    resolve_account_id,
    save_last_active_account,
)
from app.broadcast.asset_bundles import AssetBundle, BundleItem, delete_bundle, get_bundle, list_bundles, save_bundle
from app.broadcast.context import AppContext
from app.broadcast.dto import AssetRowDTO, ContactRowDTO, PlanContact
from app.broadcast.library import TextTemplate, VoiceLibrary, VoiceLibraryEntry
from app.broadcast.paths import (
    DEFAULT_ACCOUNT_ID,
    VOICE_INDEX_FILE,
    VOICE_LIB_ROOT,
    VOICE_LIB_ROOT_FALLBACK,
    account_contacts_cache_path,
    account_plans_dir,
    account_reports_dir,
    account_voice_index_path,
)
from app.broadcast.plan import (
    BroadcastPlan,
    DEFAULT_CONTACT_GAP,
    DEFAULT_MSG_GAP,
    load_plan,
    save_plan,
    step_status,
)
from app.broadcast.perturb import create_perturbed_copy
from app.broadcast.preview import PreviewService, decode_silk
from app.broadcast.report_store import ReportStore, ReportView
from app.broadcast.schedule_spec import AfterSpec, AtSpec, CronSpec, ImmediateSpec, ScheduleSpec
from app.broadcast.scheduler import BroadcastScheduler, ScheduleEntry, parse_cron_expr
from app.broadcast.sop_store import SopTemplate, delete_sop, list_sops, load_sop, resolve_contacts_by_labels, save_sop
from app.broadcast.runner import BroadcastRunner, RunRecord, RunSummary

__all__ = [
    "VoiceLibrary",
    "VoiceLibraryEntry",
    "TextTemplate",
    "PlanContact",
    "ContactRowDTO",
    "AssetRowDTO",
    "DEFAULT_ACCOUNT_ID",
    "VOICE_LIB_ROOT",
    "VOICE_LIB_ROOT_FALLBACK",
    "VOICE_INDEX_FILE",
    "get_current_account_id",
    "resolve_account_id",
    "load_last_active_account",
    "save_last_active_account",
    "AppContext",
    "AssetBundle",
    "BundleItem",
    "data_root_for",
    "account_voice_index_path",
    "account_contacts_cache_path",
    "account_plans_dir",
    "account_reports_dir",
    "BroadcastPlan",
    "DEFAULT_MSG_GAP",
    "DEFAULT_CONTACT_GAP",
    "load_plan",
    "save_plan",
    "step_status",
    "create_perturbed_copy",
    "PreviewService",
    "decode_silk",
    "ReportStore",
    "ReportView",
    "ScheduleSpec",
    "ImmediateSpec",
    "AfterSpec",
    "AtSpec",
    "CronSpec",
    "BroadcastScheduler",
    "ScheduleEntry",
    "parse_cron_expr",
    "SopTemplate",
    "list_sops",
    "load_sop",
    "save_sop",
    "delete_sop",
    "resolve_contacts_by_labels",
    "list_bundles",
    "save_bundle",
    "delete_bundle",
    "get_bundle",
    "BroadcastRunner",
    "RunRecord",
    "RunSummary",
]
