"""Authoritative data model and built-in registry for the deeper-research dispatcher.

Defines the ``Provider`` and ``AgentType`` dataclasses, built-in provider spec
templates, built-in agent types, the default provider↔agent pairing, and default
pricing.  The TOML/.env loader and assignment engine in later modules consume these
definitions to materialise runtime instances.
"""

import math
import os
import random
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Provider:
    name: str
    api_type: str                       # "openai" | "anthropic" | "gemini" | "cli"
    api_key: str
    model: str
    base_url: str | None = None
    max_tokens: int = 32768
    capabilities: tuple[str, ...] = ()
    pricing: dict | None = None         # {"in","out", optional "reasoning","searches_per_run","search_per_k"}
    fallback_models: tuple[str, ...] = ()
    max_concurrency: int | None = None
    command: str | None = None          # CLI binary name/path for cli providers
    extra_args: tuple[str, ...] = ()   # opt-in extra CLI flags (e.g. to enable web search)
    family: str = "other"               # model lineage/vendor family (e.g. "anthropic", "openai")


@dataclass(frozen=True)
class AgentType:
    name: str
    strategy: str
    system_prompt: str
    provider: str | None = None         # explicit mapping override
    requires_web_search: bool = False


@dataclass(frozen=True)
class HostSpec:
    name: str
    markers: tuple[str, ...]
    provider_name: str
    command: str
    family: str


# Ordered deliberately: Codex must win when both runtime marker families exist.
HOST_SPECS: tuple[HostSpec, ...] = (
    HostSpec("codex", ("CODEX_SESSION_ID",), "codex-sub", "codex", "openai"),
    HostSpec("claude", ("CLAUDECODE",), "claude-sub", "claude", "anthropic"),
)


# --- System prompts ---

_GENERIC_SYS = ("You are a deep research analyst producing comprehensive, fact-checked, "
                "evidence-based research reports with full citations. Produce the COMPLETE "
                "report in a single response. Do NOT ask for confirmation or suggest splitting "
                "into parts. Write the full report now.")
_REALTIME_SYS = ("You are a deep research analyst. Use your web search capabilities extensively "
                 "to find and cite real, verifiable sources. Every claim must have a citation.")
_CONTRARIAN_SYS = ("You are a deep research analyst producing comprehensive, fact-checked, "
                   "evidence-based research reports with full citations. Challenge conventional "
                   "narratives where evidence warrants it.")

# --- Strategy prompts (originally lifted from dispatch.py's STRATEGIES; this is now the source of truth) ---

_STRATEGY_ACADEMIC = """Academic Deep Dive — focus on the most-cited academic papers, NBER/SSRN working papers,
journal articles, university research, think tank publications, and review articles.
Find the canonical authors in the field. Follow citation chains. Identify theoretical
frameworks and empirical debates. Structure as a literature review with theoretical
underpinnings and empirical findings."""

_STRATEGY_PRACTITIONER = """Practitioner & Explainer — focus on practical, applied, how-it-works sources.
Industry white papers, consulting reports, trade publications, professional guides,
technical documentation, methodology documents, company reports. Find the best
explainers and how-to guides. Include data tables, process descriptions, and
real-world examples. Structure for a practitioner audience."""

_STRATEGY_REALTIME = """Real-Time Web Intelligence — focus on current, up-to-date information.
Search extensively for recent sources (last 1-3 years). Find recent news articles,
government reports, regulatory filings, press releases, current data, recent
conference proceedings. Identify what has changed recently, current controversies,
recent policy changes, emerging trends. Verify current figures and statistics.
Structure around current state and recent developments."""

_STRATEGY_GREY_LITERATURE = """Grey Literature & Primary Sources — focus on primary documents and original data.
Government reports, international organization publications (UN, World Bank, IMF, OECD),
NGO reports, official datasets, legal documents, treaties, standards, congressional
testimony, regulatory dockets. Find the PRIMARY source behind secondary claims.
If a paper cites a government report, find the report. Structure around documentary
evidence and original-source citations."""

_STRATEGY_CONTRARIAN = """Contrarian & Cross-Disciplinary Analysis — challenge conventional narratives.
Search for dissenting academic views, minority positions in policy debates,
cross-disciplinary insights (e.g., complexity science applied to markets, network
theory applied to supply chains), unconventional data sources, and perspectives
from outside the mainstream Western institutional framework. Find what the other
research strategies are likely to miss. Structure around alternative framings,
overlooked evidence, and underrepresented perspectives."""


# --- Built-in registries ---

BUILTIN_AGENT_TYPES: dict[str, AgentType] = {
    "academic": AgentType(
        name="academic",
        strategy=_STRATEGY_ACADEMIC,
        system_prompt=_GENERIC_SYS,
        requires_web_search=False,
    ),
    "practitioner": AgentType(
        name="practitioner",
        strategy=_STRATEGY_PRACTITIONER,
        system_prompt=_GENERIC_SYS,
        requires_web_search=False,
    ),
    "real-time": AgentType(
        name="real-time",
        strategy=_STRATEGY_REALTIME,
        system_prompt=_REALTIME_SYS,
        requires_web_search=True,
    ),
    "grey-literature": AgentType(
        name="grey-literature",
        strategy=_STRATEGY_GREY_LITERATURE,
        system_prompt=_GENERIC_SYS,
        requires_web_search=False,
    ),
    "contrarian": AgentType(
        name="contrarian",
        strategy=_STRATEGY_CONTRARIAN,
        system_prompt=_CONTRARIAN_SYS,
        requires_web_search=False,
    ),
}

DEFAULT_PAIRING: dict[str, str] = {
    "academic": "claude",
    "practitioner": "chatgpt",
    "grey-literature": "gemini",
    "contrarian": "grok",
    # No built-in ships `web_search`, so the `real-time` agent has no default pairing:
    # it degrades to knowledge-cutoff results unless the user configures a TOML
    # web-search provider (Round-1 live retrieval is Exa slices, not the agent).
}

# Spec templates keyed by built-in provider name.  The loader materialises them into
# runtime Provider instances once an API key is resolved from the environment, which
# is why api_key isn't stored here (and why Provider has no consumer in this file).
BUILTIN_PROVIDER_SPECS: dict[str, dict] = {
    "claude": {
        "api_type": "anthropic",
        "family": "anthropic",
        # Earlier defaults (`claude-opus-4-20250514`, then `claude-opus-4-8`) went
        # stale as model IDs rolled; current Opus is `claude-opus-5`, $5/$25 per MTok.
        # Override in TOML as model IDs drift.
        "model": "claude-opus-5",
        "env_key": "ANTHROPIC_API_KEY",
        "max_tokens": 128000,
        "capabilities": [],
        "pricing": {"in": 5.0, "out": 25.0},
    },
    "chatgpt": {
        "api_type": "openai",
        "family": "openai",
        "env_key": "OPENAI_API_KEY",
        "model": "gpt-4.1",
        "max_tokens": 32768,
        "capabilities": [],
        "pricing": {"in": 2.0, "out": 8.0},
    },
    "gemini": {
        "api_type": "gemini",
        "family": "google",
        "env_key": "GOOGLE_API_KEY",
        "model": "gemini-2.5-pro",
        "fallback_models": ["gemini-2.5-flash", "gemini-2.0-flash"],
        "max_tokens": 65536,
        "capabilities": [],
        "pricing": {"in": 1.25, "out": 10.0},
    },
    "grok": {
        "api_type": "openai",
        "family": "xai",
        "env_key": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "model": "grok-3-latest",
        "max_tokens": 128000,
        "capabilities": [],
        "pricing": {"in": 3.0, "out": 15.0},
    },
}

# Convenience reference consumed by the cost-estimation module.
DEFAULT_PRICING = {name: spec["pricing"] for name, spec in BUILTIN_PROVIDER_SPECS.items()}

# OpenAI and xAI remain distinct vendor/model families, but their shared-corpus risk
# makes either one unsuitable as an independent adversary for the other.
_ADVERSARY_EXCLUDED_FAMILY_PAIRS = frozenset({frozenset(("openai", "xai"))})


class ConfigError(Exception):
    pass


def _host_spec(name: str | None) -> HostSpec | None:
    return next((spec for spec in HOST_SPECS if spec.name == name), None)


def detect_host(env: Mapping[str, str] | None = None) -> str | None:
    """Return the invoking runtime name, preferring Codex when markers overlap."""
    environment = os.environ if env is None else env
    for spec in HOST_SPECS:
        if any(environment.get(marker) for marker in spec.markers):
            return spec.name
    return None


def _has_web_search(provider):
    return "web_search" in provider.capabilities


def assign(agents, providers, seed=0, existing=None):
    """Return (assignments {agent_type: provider}, warnings). `existing` short-circuits (resume)."""
    if existing is not None:
        return dict(existing), []
    if not providers:
        raise ConfigError("No providers available — set at least one API key or define a [providers.*] block.")
    warnings = []
    assignments = {}
    unassigned = []

    # 1. explicit mappings
    for name, at in agents.items():
        if at.provider is not None:
            if at.provider not in providers:
                raise ConfigError(f"Agent '{name}' maps to undefined provider '{at.provider}'.")
            if at.requires_web_search and not _has_web_search(providers[at.provider]):
                raise ConfigError(f"Agent '{name}' requires web search but provider "
                                  f"'{at.provider}' lacks the 'web_search' capability.")
            assignments[name] = at.provider
        else:
            unassigned.append(name)

    # 2. built-in default pairing
    still = []
    for name in unassigned:
        target = DEFAULT_PAIRING.get(name)
        if target and target in providers:
            at = agents[name]
            if not at.requires_web_search or _has_web_search(providers[target]):
                assignments[name] = target
                continue
        still.append(name)
    unassigned = still

    # 3. web-search agents -> a web-search-capable provider (sorted by name)
    searchers = [p for p in providers.values() if _has_web_search(p)]
    searchers.sort(key=lambda p: p.name)
    still = []
    for name in unassigned:
        if agents[name].requires_web_search and searchers:
            assignments[name] = searchers[0].name
        else:
            still.append(name)
    unassigned = still

    # 4. seeded round-robin fill (deterministic), warning if a web-search agent lands on a plain provider
    pool = sorted(providers)
    rng = random.Random(seed)
    rng.shuffle(pool)
    for i, name in enumerate(sorted(unassigned)):
        chosen = pool[i % len(pool)]
        assignments[name] = chosen
        if agents[name].requires_web_search and not _has_web_search(providers[chosen]):
            warnings.append(f"Agent '{name}' needs live web search but no web_search-capable "
                            f"provider is configured; '{chosen}' will return knowledge-cutoff results.")
    return assignments, warnings


def resolve_assignments(agents, providers, seed=0, prior_assignments=None):
    """assign(), but transparently recompute if a resumed map is stale
    (references a missing provider or omits a now-active agent)."""
    assignments, warnings = assign(agents, providers, seed=seed, existing=prior_assignments)
    stale = (any(name not in assignments for name in agents)
             or any(p not in providers for p in assignments.values())
             or any(name not in agents for name in assignments))
    if prior_assignments is not None and stale:
        assignments, warnings = assign(agents, providers, seed=seed, existing=None)
        warnings = list(warnings) + ["resume assignments stale (provider/agent set changed); recomputed."]
    return assignments, warnings


def default_toml_paths():
    """Return the list of existing TOML config paths in standard locations."""
    candidates = [Path.home() / ".config" / "deeper-research" / "config.toml", Path("deeper-research.toml")]
    return [p for p in candidates if p.exists()]


def load_env_files(paths=(Path.home() / ".env", Path(".env")), env=None):
    """Merge KEY=VAL lines from .env files into a dict (mirrors old dispatch.py loader)."""
    env = dict(os.environ if env is None else env)
    for ep in paths:
        ep = Path(ep)
        if not ep.exists():
            continue
        for line in ep.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip("'\"")
                if k and v and k not in env:  # skip blanks/empties; don't override keys already set
                    env[k] = v
    return env

def _provider_from_table(name, t, env):
    if t.get("api_type") == "cli":
        if "command" not in t:
            raise ConfigError(f"provider '{name}' with api_type='cli' missing required field: command")
        if shutil.which(t["command"]) is None:
            return None  # binary not installed — skip gracefully
        return Provider(
            name=name, api_type="cli", api_key="", model=t.get("model", ""),
            base_url=t.get("base_url"), max_tokens=t.get("max_tokens", 32768),
            capabilities=tuple(t.get("capabilities", ())), pricing=t.get("pricing"),
            fallback_models=tuple(t.get("fallback_models", ())),
            max_concurrency=t.get("max_concurrency"),
            command=t["command"],
            extra_args=tuple(t.get("extra_args", ())),
            family=t.get("family", "other"),
        )
    missing = {"api_type", "model"} - t.keys()
    if missing:
        raise ConfigError(f"provider '{name}' missing required field(s): {', '.join(sorted(missing))}")
    if "api_key" in t:
        key = t["api_key"]
    elif "api_key_env" in t:
        key = env.get(t["api_key_env"], "")
    else:
        key = ""
    if not key:
        return None
    return Provider(
        name=name, api_type=t["api_type"], api_key=key, model=t["model"],
        base_url=t.get("base_url"), max_tokens=t.get("max_tokens", 32768),
        capabilities=tuple(t.get("capabilities", ())), pricing=t.get("pricing"),
        fallback_models=tuple(t.get("fallback_models", ())),
        max_concurrency=t.get("max_concurrency"),
        family=t.get("family", "other"),
    )

def _builtin_provider(name, env):
    spec = BUILTIN_PROVIDER_SPECS[name]
    key = env.get(spec["env_key"], "")
    if not key:
        return None
    return Provider(
        name=name, api_type=spec["api_type"], api_key=key, model=spec["model"],
        base_url=spec.get("base_url"), max_tokens=spec.get("max_tokens", 32768),
        capabilities=tuple(spec.get("capabilities", ())), pricing=spec.get("pricing"),
        fallback_models=tuple(spec.get("fallback_models", ())),
        family=spec.get("family", "other"),
    )

def load_defaults(toml_paths):
    """Read the [defaults] table (provider roles like utility/synthesis) from the TOML
    paths; later paths override earlier. Returns {} if no [defaults] anywhere."""
    out = {}
    for path in toml_paths:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        out.update(data.get("defaults") or {})
    return out


def pick_provider(providers, role, defaults, fallback=("claude-sub", "claude", "chatgpt"),
                  host=None):
    """Resolve a provider for a one-off role (e.g. 'utility'): the [defaults][role]
    provider if configured and available; an unavailable explicit selection is an
    error. Otherwise use the first available provider in `fallback`, then any
    available provider, else None."""
    name = defaults.get(role)
    if name:
        if name not in providers:
            raise ConfigError(
                f"Explicit provider '{name}' for role '{role}' is not available.")
        return providers[name]
    if not providers:
        return None
    host_spec = _host_spec(detect_host() if host is None else host)
    if host_spec is not None and host_spec.provider_name in providers:
        return providers[host_spec.provider_name]
    for fb in fallback:
        if fb in providers:
            return providers[fb]
    return next(iter(providers.values()), None)


def load_config(toml_paths, env):
    providers = {n: p for n in BUILTIN_PROVIDER_SPECS if (p := _builtin_provider(n, env))}
    host_spec = _host_spec(detect_host(env))
    if host_spec is not None and shutil.which(host_spec.command) is not None:
        providers[host_spec.provider_name] = Provider(
            name=host_spec.provider_name, api_type="cli", api_key="", model="",
            command=host_spec.command, family=host_spec.family,
        )
    agents = dict(BUILTIN_AGENT_TYPES)
    for path in toml_paths:                          # later paths override earlier
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        for name, t in (data.get("providers") or {}).items():
            p = _provider_from_table(name, t, env)
            if p is not None:
                providers[name] = p
            else:
                providers.pop(name, None)  # explicit unavailable entries override implicit providers
        for name, t in (data.get("agents") or {}).items():
            base = agents.get(name)
            agents[name] = AgentType(
                name=name,
                strategy=t.get("strategy", base.strategy if base else ""),
                system_prompt=t.get("system_prompt", base.system_prompt if base else _GENERIC_SYS),
                provider=t.get("provider", base.provider if base else None),
                requires_web_search=t.get("requires_web_search",
                                          base.requires_web_search if base else False),
            )
    return providers, agents


# ---------------------------------------------------------------------------
# Run configuration — Round-1 slice roster + adversary selection (v2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SliceSpec:
    query: str
    category: str | None                # XOR with include_domains (Exa contract)
    include_domains: tuple[str, ...] | None   # tuple: real frozenness + hashability, no shared-mutable leak
    enabled: bool


@dataclass(frozen=True)
class RunConfig:
    mode: str                           # "slices" is the only valid mode
    max_retrieval_usd: float
    min_evidence_total: int
    min_nonempty_slices: int
    slices: dict[str, SliceSpec]
    adversary_chain: list[str]          # as configured; walked for selection
    adversary: str                      # resolved adversary provider name
    synthesizer: str                    # resolved synthesis provider name
    adversary_warning: str | None
    # Per-run USD cap for the one Exa Agent "ultra" scout (agent_scout.py).
    # Separate from max_retrieval_usd; 0 turns the scout off.
    agent_scout_usd: float = 10.0


# Ship defaults for the Round-1 slice roster when no [slices.*] table is present.
# publication/news use Exa `category`; institutional/personal-site use `include_domains`.
_DEFAULT_SLICES: dict[str, SliceSpec] = {
    "publication": SliceSpec(
        query="{topic}", category="research paper",
        include_domains=None, enabled=True),
    "news": SliceSpec(
        query="{topic} latest developments", category="news",
        include_domains=None, enabled=True),
    "institutional": SliceSpec(
        query="{topic} policy report analysis", category=None,
        include_domains=("oecd.org", "worldbank.org", "imf.org", "un.org", "brookings.edu"),
        enabled=True),
    "financial": SliceSpec(
        query="{topic} financial analysis", category="financial report",
        include_domains=None, enabled=False),
    "personal-site": SliceSpec(
        query="{topic}", category=None,
        include_domains=("substack.com", "medium.com"), enabled=False),
}


def _slice_from_table(name, t):
    category = t.get("category")
    include_domains = t.get("include_domains")
    if category is not None and include_domains is not None:
        raise ValueError(
            f"slice '{name}' sets both category and include_domains — they are XOR "
            f"(Exa accepts one filter axis, not both).")
    if include_domains is not None:
        include_domains = tuple(include_domains)
    return SliceSpec(
        query=t.get("query", "{topic}"),
        category=category,
        include_domains=include_domains,
        enabled=bool(t.get("enabled", True)),
    )


def _resolve_adversary(chain, providers, synthesizer_name, host=None):
    """Walk the configured chain; pick the first configured+available provider whose
    family is independent from the actual synthesizer. Skip chain entries naming no configured provider.
    If none qualify, warn and fall back to the synthesizer provider name."""
    synthesizer = providers.get(synthesizer_name)
    host_spec = _host_spec(detect_host() if host is None else host)
    synthesizer_family = (synthesizer.family if synthesizer is not None
                          else host_spec.family if host_spec is not None else "anthropic")
    skipped = []
    for entry in chain:
        prov = providers.get(entry)
        if prov is None:
            skipped.append(entry)
            continue
        family_pair = frozenset((prov.family, synthesizer_family))
        if (prov.family == synthesizer_family
                or family_pair in _ADVERSARY_EXCLUDED_FAMILY_PAIRS):
            continue
        return entry, None
    reasons = []
    if skipped:
        reasons.append(f"chain entries not configured: {', '.join(skipped)}")
    reasons.append(f"no configured provider independent from synthesizer family "
                   f"'{synthesizer_family}' available to act as adversary")
    warning = (f"Adversary falls back to the synthesizer '{synthesizer_name}' "
               f"(not independent from synthesis) — {'; '.join(reasons)}. "
               "Configure an independent provider for the adversary.")
    return synthesizer_name, warning


def _agent_scout_usd(run: dict) -> float:
    """[run].agent_scout_usd: a finite number >= 0 (0 = scout off)."""
    raw = run.get("agent_scout_usd", 10.0)
    if isinstance(raw, bool):
        raise ValueError("[run].agent_scout_usd must be a number, not a boolean")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"[run].agent_scout_usd must be a finite number >= 0, got {raw!r}")
    return value


def load_run_config(toml_paths=None, env=None):
    """Load the [run] + [slices.*] tables into a RunConfig.

    Mirrors load_config's injectable signature. Honors DEEPER_RESEARCH_CONFIG (in env)
    as a TOML source override. Absent [run] ⇒ all defaults; invalid mode ⇒ ValueError."""
    env = dict(os.environ if env is None else env)
    override = env.get("DEEPER_RESEARCH_CONFIG")
    if override:
        toml_paths = [Path(override)]
    elif toml_paths is None:
        toml_paths = default_toml_paths()

    run: dict = {}
    slice_tables: dict[str, dict] = {}
    for path in toml_paths:                          # later paths override earlier
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        run.update(data.get("run") or {})
        for sname, t in (data.get("slices") or {}).items():
            slice_tables[sname] = t

    mode = run.get("mode", "slices")
    if mode != "slices":
        raise ValueError(f"invalid run mode '{mode}' — only 'slices' is supported.")

    if slice_tables:
        slices = {n: _slice_from_table(n, t) for n, t in slice_tables.items()}
    else:
        slices = dict(_DEFAULT_SLICES)

    adversary_chain = list(run.get("adversary", ["grok", "chatgpt", "gemini"]))
    if not adversary_chain:                          # never empty
        adversary_chain = ["grok", "chatgpt", "gemini"]

    # Adversary selection runs against configured + available providers.
    providers, _ = load_config(toml_paths, env)
    defaults = load_defaults(toml_paths)
    host = detect_host(env)
    synthesizer = pick_provider(providers, "synthesis", defaults, host=host)
    synthesizer_name = synthesizer.name if synthesizer is not None else "claude"
    adversary, adversary_warning = _resolve_adversary(
        adversary_chain, providers, synthesizer_name, host=host)

    return RunConfig(
        mode=mode,
        max_retrieval_usd=float(run.get("max_retrieval_usd", 1.0)),
        min_evidence_total=int(run.get("min_evidence_total", 10)),
        min_nonempty_slices=int(run.get("min_nonempty_slices", 2)),
        slices=slices,
        adversary_chain=adversary_chain,
        adversary=adversary,
        synthesizer=synthesizer_name,
        adversary_warning=adversary_warning,
        agent_scout_usd=_agent_scout_usd(run),
    )
