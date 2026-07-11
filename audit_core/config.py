import os

from dotenv import load_dotenv


load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(BASE_DIR, "inbox")
OUTPUT = os.path.join(BASE_DIR, "output")
ARCHIVE = os.path.join(BASE_DIR, "archive")
FAILED = os.path.join(BASE_DIR, "failed")
CONFIG_DIR = os.path.join(BASE_DIR, "config", "compliance_rules")
SEMICONDUCTOR_IP_CONFIG_DIR = os.path.join(BASE_DIR, "config", "semiconductor_ip_rules")
VECTOR_STORE_DIR = os.path.join(BASE_DIR, "vector_store")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
AUDIT_HISTORY_FILENAME = "audit_history.jsonl"

COMPLIANCE_MODE = "compliance"
SEMICONDUCTOR_IP_MODE = "semiconductor_ip"
SUPPORTED_AUDIT_MODES = {COMPLIANCE_MODE, SEMICONDUCTOR_IP_MODE}

EMBEDDING_CONCURRENCY = int(os.getenv("EMBEDDING_CONCURRENCY", "2"))
EMBEDDING_MAX_RETRIES = int(os.getenv("EMBEDDING_MAX_RETRIES", "3"))

AUDIT_MODEL = os.getenv("AUDIT_MODEL", "qwen3.5:9b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.5"))
AUDIT_INPUT_TOKEN_LIMIT = int(os.getenv("AUDIT_INPUT_TOKEN_LIMIT", "6000"))
SEMICONDUCTOR_IP_INPUT_TOKEN_LIMIT = int(
    os.getenv("SEMICONDUCTOR_IP_INPUT_TOKEN_LIMIT", "2500")
)
SEMICONDUCTOR_IP_NUM_PREDICT = int(
    os.getenv("SEMICONDUCTOR_IP_NUM_PREDICT", "900")
)

POLL_INTERVAL = 0.5


def normalize_audit_mode(mode: str | None) -> str:
    selected = (mode or os.getenv("AUDIT_MODE") or COMPLIANCE_MODE).strip()
    if selected not in SUPPORTED_AUDIT_MODES:
        raise ValueError(
            f"Unsupported audit mode: {selected}. "
            f"Expected one of: {', '.join(sorted(SUPPORTED_AUDIT_MODES))}"
        )
    return selected


def rules_dir_for_mode(mode: str) -> str:
    if normalize_audit_mode(mode) == SEMICONDUCTOR_IP_MODE:
        return SEMICONDUCTOR_IP_CONFIG_DIR
    return CONFIG_DIR


def collection_name_for_mode(mode: str) -> str:
    if normalize_audit_mode(mode) == SEMICONDUCTOR_IP_MODE:
        return "semiconductor_ip_rules"
    return "compliance_rules"


def input_token_limit_for_mode(mode: str) -> int:
    if normalize_audit_mode(mode) == SEMICONDUCTOR_IP_MODE:
        return SEMICONDUCTOR_IP_INPUT_TOKEN_LIMIT
    return AUDIT_INPUT_TOKEN_LIMIT


def generation_options_for_mode(mode: str) -> dict[str, int | float]:
    options: dict[str, int | float] = {
        "temperature": 0.1,
        "num_ctx": 8192,
        "num_keep": 0,
    }
    if normalize_audit_mode(mode) == SEMICONDUCTOR_IP_MODE:
        options["num_predict"] = SEMICONDUCTOR_IP_NUM_PREDICT
    return options
