from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
import urllib.error
from pathlib import Path

from jev_context_router.config import Settings
from jev_context_router.models import Selection
from jev_context_router.providers import JevSelector, LocalSelector
from jev_context_router.router import ContextRouter

TRANSVERSAL_QUERY = (
    "Debug the JavaScript/TypeScript Brain Dashboard sync flow. Trace how the POST sync endpoint "
    "reads and validates the upstream JSON export, writes tasks in the Prisma transaction, marks "
    "stale tasks, records errors, and how the ICS export consumes the data. Identify all relevant "
    "files/functions, then find one concrete consistency or data-loss bug across this flow and explain "
    "a minimal fix. Do not edit files."
)
FRENCH_QUERY = (
    "Debug une anomalie de qualité de données : retrouver dans le dépôt le pipeline qui collecte "
    "des organisations de santé, filtre Marseille et au moins trois praticiens, enrichit les numéros "
    "de téléphone, puis exporte CSV et JSON. Identifier les fichiers et fonctions sans les modifier."
)
EXPECTED = {
    "brain-dashboard/app/api/sync/route.ts",
    "brain-dashboard/lib/sync.ts",
    "brain-dashboard/lib/tasks.ts",
    "brain-dashboard/lib/ics.ts",
}
IRRELEVANT = {
    ".opencode/test-vibe.js",
    "brain-dashboard/scripts/generate-tasks-from-plan.mjs",
    "brain-dashboard/scripts/reset-tasks.mjs",
}


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_fixture(root: Path) -> None:
    (root / ".git").mkdir()
    _write(root, "brain-dashboard/app/api/sync/route.ts", """import { syncTasks } from '../../../lib/sync';
export async function POST(request: Request) {
  const upstream = await request.json();
  return syncTasks(upstream);
}
""")
    _write(root, "brain-dashboard/lib/sync.ts", """import { prisma } from './db';
export async function syncTasks(upstream: TaskExport) {
  return prisma.$transaction(async tx => {
    await tx.task.updateMany({ data: { stale: true } });
    for (const task of upstream.tasks) await tx.task.upsert({ where: { id: task.id }, update: { ...task, stale: false }, create: task });
    await tx.syncRun.create({ data: { status: 'ok' } });
  });
}
""")
    _write(root, "brain-dashboard/lib/tasks.ts", """import { prisma } from './db';
export async function visibleTasks() {
  return prisma.task.findMany({ where: { stale: false } });
}
""")
    _write(root, "brain-dashboard/lib/ics.ts", """import { prisma } from './db';
export async function exportIcs() {
  const tasks = await prisma.task.findMany(); // BUG: stale tasks are still exported
  return tasks.map(task => `BEGIN:VEVENT\\nSUMMARY:${task.title}\\nEND:VEVENT`).join('\\n');
}
""")
    _write(root, "brain-dashboard/prisma/schema.prisma", "model Task {\n  id String @id\n  title String\n  stale Boolean @default(false)\n}\n")
    _write(root, ".opencode/test-vibe.js", "export function vibeTest() { return 'unrelated'; }\n")
    _write(root, "brain-dashboard/scripts/generate-tasks-from-plan.mjs", "export function generatePlanTasks() { return []; }\n")
    _write(root, "brain-dashboard/scripts/reset-tasks.mjs", "export function resetDemoTasks() { return []; }\n")
    _write(root, "scripts/scrape-doctolib-structures.js", """export async function scrapeHealthcareProviders() {
  const healthcareProviders = await collectOrganizations('ORGANIZATION');
  const marseille = healthcareProviders.filter(item => item.city === 'Marseille' && item.estimatedPractitioners >= 3);
  const enriched = marseille.map(getPhoneNumber);
  writeFileSync('providers.csv', toCsv(enriched));
  writeFileSync('providers.json', JSON.stringify(enriched));
}
""")
    for index in range(30):
        _write(root, f"src/unrelated-{index}.ts", f"export function unrelatedFeature{index}() {{ return {index}; }}\n")


class SimulatedAvailableSelector(JevSelector):
    def __init__(self, settings: Settings):
        super().__init__("benchmark-placeholder", settings)

    def _evaluate(self, state, questions):
        self.last_payload_bytes = len(json.dumps(
            {"state": state, "model": self.settings.model, "questions": questions},
            separators=(",", ":"),
        ).encode())
        answers = {}
        doctolib_request = "doctolib" in state.get("request", "").lower() or "marseille" in state.get("request", "").lower()
        for index, item in state.get("candidate_symbols", {}).items():
            useful = item["path"] == "scripts/scrape-doctolib-structures.js" if doctolib_request else item["path"] in EXPECTED
            answers[f"fit_{index}"] = {"noul": 0.95 if useful else 0.05}
        return answers, {"input_tokens": math.ceil(self.last_payload_bytes / 4), "output_tokens": len(answers) * 3}, 0.002


class SimulatedUnavailableSelector(JevSelector):
    def __init__(self, settings: Settings):
        super().__init__("benchmark-placeholder", settings)

    def select_symbols(self, query, repository, candidates, settings):
        raise urllib.error.URLError("offline benchmark")


def summarize(name: str, result, elapsed: float) -> dict:
    metrics = result.metrics
    included = set(metrics.get("included_files", ()))
    expected = {"scripts/scrape-doctolib-structures.js"} if name in {"simple-targeted", "french"} else EXPECTED
    expected_found = sorted(expected & included)
    irrelevant = sorted(IRRELEVANT & included)
    return {
        "scenario": name,
        "status": result.status,
        "elapsed_seconds": elapsed,
        "intent": metrics.get("intent"),
        "retrieval_mode": metrics.get("retrieval_mode"),
        "index_seconds": metrics.get("index_seconds"),
        "cache": metrics.get("cache"),
        "files_indexed": metrics.get("files_indexed"),
        "bytes_read": metrics.get("bytes_read"),
        "selector_input_tokens": metrics.get("selector_input_tokens"),
        "selector_output_tokens": metrics.get("selector_output_tokens"),
        "selector_prompt_bytes": metrics.get("selector_prompt_bytes"),
        "estimated_context_tokens": metrics.get("estimated_context_tokens"),
        "selector_token_ratio": metrics.get("selector_token_ratio"),
        "external_status": metrics.get("external_status"),
        "external_error_reason": metrics.get("external_error_reason"),
        "fallback_used": metrics.get("fallback_used"),
        "included_files": sorted(included),
        "expected_files_found": expected_found,
        "expected_recall": len(expected_found) / len(expected),
        "irrelevant_included_files": irrelevant,
        "noise_rate": len(irrelevant) / max(1, len(included)),
        "inclusion_reasons": metrics.get("inclusion_reasons", {}),
    }


def run(root: Path) -> dict:
    cache_dir = root.parent / "benchmark-cache"
    common = dict(
        workspace_roots=(root,), index_cache_dir=cache_dir, max_context_chars=16_000,
        lexical_timeout_seconds=0.2, external_timeout_seconds=0.5,
        timeout_seconds=0.4, route_timeout_seconds=5.0,
    )
    scenarios: list[tuple[str, str, Settings, object]] = [
        ("simple-targeted", "Refactor scripts/scrape-doctolib-structures.js and identify exact symbols", Settings(**common), LocalSelector()),
        ("semantic-cold", TRANSVERSAL_QUERY, Settings(**common, lexical_enabled=False), SimulatedAvailableSelector(Settings(**common, lexical_enabled=False))),
        ("semantic-warm", TRANSVERSAL_QUERY, Settings(**common, lexical_enabled=False), SimulatedAvailableSelector(Settings(**common, lexical_enabled=False))),
        ("french", FRENCH_QUERY, Settings(**common), LocalSelector()),
        ("network-unavailable", TRANSVERSAL_QUERY, Settings(**common, lexical_enabled=False), SimulatedUnavailableSelector(Settings(**common, lexical_enabled=False))),
    ]
    results = []
    for name, query, settings, selector in scenarios:
        started = time.perf_counter()
        result = ContextRouter(settings, selector).route(query, cwd=root)
        results.append(summarize(name, result, time.perf_counter() - started))
    return {
        "fixture": "synthetic-brain-dashboard",
        "network": "simulated; no credential or user repository required",
        "user_repository_modified": False,
        "scenarios": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="jev-context-benchmark-") as directory:
        root = Path(directory) / "fixture"
        root.mkdir()
        make_fixture(root)
        payload = run(root)
    scenarios = {item["scenario"]: item for item in payload["scenarios"]}
    assert all(item["status"] == "routed" for item in scenarios.values())
    assert all(item["expected_recall"] == 1.0 for item in scenarios.values())
    assert all(item["noise_rate"] <= 0.1 for item in scenarios.values())
    assert scenarios["semantic-warm"]["cache"]["status"] == "warm"
    assert scenarios["french"]["intent"]["intent"] == "code"
    assert scenarios["network-unavailable"]["elapsed_seconds"] < 2.0
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
