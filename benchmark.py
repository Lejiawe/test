from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox


STORAGE_CHOICES = ("none", "oss", "file")
DEFAULT_PROVIDER = "aliyun"
DEFAULT_MAX_WORKERS = 1000
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_url: str
    domain: str
    template: str
    sandbox_timeout: int
    vpc_config: dict | None
    oss_config: dict | None
    role_arn: str
    file_metadata_key: str
    file_config: dict | None
    extra_metadata: dict[str, str]


@dataclass
class TrialResult:
    provider: str
    storage: str
    target_rate_per_s: int
    duration_seconds: float
    configured_max_workers: int
    effective_max_workers: int
    trial_index: int
    scheduled_at_utc: str
    queue_delay_ms: float
    api_latency_ms: float | None
    first_command_latency_ms: float | None
    second_command_latency_ms: float | None
    api_success: bool
    ready_success: bool
    cleanup_success: bool
    sandbox_id: str
    failure_phase: str
    error_type: str
    error_message: str
    error_traceback: str
    cleanup_error_type: str
    cleanup_error_message: str
    cleanup_error_traceback: str


def env_json(name: str) -> dict | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} 不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} 必须是 JSON 对象")
    return value


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def load_settings(require_credentials: bool = True) -> Settings:
    load_dotenv()
    extra = env_json("E2B_EXTRA_METADATA_JSON") or {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()):
        raise RuntimeError("E2B_EXTRA_METADATA_JSON 的键和值都必须是字符串")

    return Settings(
        api_key=require_env("E2B_API_KEY") if require_credentials else "",
        api_url=require_env("E2B_API_URL") if require_credentials else "",
        domain=require_env("E2B_DOMAIN") if require_credentials else "",
        template=os.environ.get("E2B_TEMPLATE", "code-interpreter-v1").strip()
        or "code-interpreter-v1",
        sandbox_timeout=int(os.environ.get("E2B_SANDBOX_TIMEOUT", "60")),
        vpc_config=env_json("E2B_VPC_CONFIG_JSON"),
        oss_config=env_json("E2B_OSS_CONFIG_JSON"),
        role_arn=os.environ.get("E2B_ROLE_ARN", "").strip(),
        file_metadata_key=os.environ.get("E2B_FILE_METADATA_KEY", "").strip(),
        file_config=env_json("E2B_FILE_CONFIG_JSON"),
        extra_metadata=extra,
    )


def metadata_for(storage: str, settings: Settings) -> dict[str, str]:
    metadata = dict(settings.extra_metadata)
    if settings.vpc_config:
        metadata["fc.sandbox.network.vpc"] = json.dumps(
            settings.vpc_config, separators=(",", ":")
        )

    if storage == "oss":
        if not settings.oss_config:
            raise RuntimeError("OSS 场景缺少 E2B_OSS_CONFIG_JSON")
        if not settings.role_arn:
            raise RuntimeError("OSS 场景缺少 E2B_ROLE_ARN")
        metadata["fc.sandbox.storage.oss"] = json.dumps(
            settings.oss_config, separators=(",", ":")
        )
        metadata["fc.sandbox.auth.role"] = settings.role_arn
    elif storage == "file":
        if not settings.file_metadata_key or not settings.file_config:
            raise RuntimeError(
                "文件存储场景缺少 E2B_FILE_METADATA_KEY 或 E2B_FILE_CONFIG_JSON"
            )
        metadata[settings.file_metadata_key] = json.dumps(
            settings.file_config, separators=(",", ":")
        )
    elif storage != "none":
        raise ValueError(f"未知存储类型: {storage}")
    return metadata


def one_trial(
    *,
    settings: Settings,
    provider: str,
    storage: str,
    rate: int,
    duration_seconds: float,
    configured_max_workers: int,
    effective_max_workers: int,
    trial_index: int,
    scheduled_monotonic: float,
    scheduled_at_utc: str,
) -> TrialResult:
    sandbox = None
    cleanup_success = True
    sandbox_id = ""
    api_latency_ms = None
    first_command_latency_ms = None
    second_command_latency_ms = None
    api_success = False
    ready_success = False
    failure_phase = ""
    error_type = ""
    error_message = ""
    error_traceback = ""
    cleanup_error_type = ""
    cleanup_error_message = ""
    cleanup_error_traceback = ""

    actual_start = time.perf_counter()
    queue_delay_ms = max(0.0, (actual_start - scheduled_monotonic) * 1000)

    current_phase = "api_create"
    try:
        create_start = time.perf_counter()
        sandbox = Sandbox.create(
            template=settings.template,
            timeout=settings.sandbox_timeout,
            api_key=settings.api_key,
            api_url=settings.api_url,
            domain=settings.domain,
            metadata=metadata_for(storage, settings),
        )
        api_return = time.perf_counter()
        api_latency_ms = (api_return - create_start) * 1000
        api_success = True
        sandbox_id = str(
            getattr(sandbox, "sandbox_id", getattr(sandbox, "id", ""))
        )

        current_phase = "first_command"
        first_result = sandbox.commands.run(
            "python3 -c \"print('ALIYUN_SANDBOX_FIRST_COMMAND')\"",
            timeout=30,
        )
        first_command_ready = time.perf_counter()
        first_command_latency_ms = (first_command_ready - create_start) * 1000
        first_stdout = getattr(first_result, "stdout", "") or ""
        first_command_success = "ALIYUN_SANDBOX_FIRST_COMMAND" in first_stdout
        if not first_command_success:
            failure_phase = "first_command"
            error_type = "FirstCommandCheckFailed"
            error_message = "首条命令未返回预期标记"
        else:
            current_phase = "second_command"
            second_result = sandbox.commands.run(
                "python3 -c \"print('ALIYUN_SANDBOX_SECOND_COMMAND')\"",
                timeout=30,
            )
            second_command_ready = time.perf_counter()
            second_command_latency_ms = (second_command_ready - create_start) * 1000
            second_stdout = getattr(second_result, "stdout", "") or ""
            ready_success = "ALIYUN_SANDBOX_SECOND_COMMAND" in second_stdout
            if not ready_success:
                failure_phase = "second_command"
                error_type = "SecondCommandCheckFailed"
                error_message = "第二条命令未返回预期标记"
    except Exception as exc:  # 保留单次失败，不中断整轮测试。
        failure_phase = current_phase
        error_type = type(exc).__name__
        error_message = str(exc).replace("\r", " ").replace("\n", " ")[:1000]
        error_traceback = traceback.format_exc()
    finally:
        if sandbox is not None:
            try:
                cleanup_success = bool(sandbox.kill())
                if not cleanup_success:
                    cleanup_error_type = "CleanupReturnedFalse"
                    cleanup_error_message = "sandbox.kill() 返回 False"
            except Exception as exc:
                cleanup_success = False
                cleanup_error_type = type(exc).__name__
                cleanup_error_message = (
                    str(exc).replace("\r", " ").replace("\n", " ")[:1000]
                )
                cleanup_error_traceback = traceback.format_exc()

    return TrialResult(
        provider=provider,
        storage=storage,
        target_rate_per_s=rate,
        duration_seconds=(
            int(duration_seconds)
            if float(duration_seconds).is_integer()
            else duration_seconds
        ),
        configured_max_workers=configured_max_workers,
        effective_max_workers=effective_max_workers,
        trial_index=trial_index,
        scheduled_at_utc=scheduled_at_utc,
        queue_delay_ms=round(queue_delay_ms, 3),
        api_latency_ms=round(api_latency_ms, 3) if api_latency_ms is not None else None,
        first_command_latency_ms=(
            round(first_command_latency_ms, 3)
            if first_command_latency_ms is not None
            else None
        ),
        second_command_latency_ms=(
            round(second_command_latency_ms, 3)
            if second_command_latency_ms is not None
            else None
        ),
        api_success=api_success,
        ready_success=ready_success,
        cleanup_success=cleanup_success,
        sandbox_id=sandbox_id,
        failure_phase=failure_phase,
        error_type=error_type,
        error_message=error_message,
        error_traceback=error_traceback,
        cleanup_error_type=cleanup_error_type,
        cleanup_error_message=cleanup_error_message,
        cleanup_error_traceback=cleanup_error_traceback,
    )


def run_rate(
    settings: Settings,
    provider: str,
    storage: str,
    rate: int,
    duration_seconds: float,
    max_workers: int,
) -> list[TrialResult]:
    attempts = max(1, math.ceil(rate * duration_seconds))
    effective_max_workers = min(max_workers, attempts)
    futures: list[Future[TrialResult]] = []
    run_start = time.perf_counter() + 0.25
    utc_start = datetime.now(timezone.utc).timestamp() + 0.25

    with ThreadPoolExecutor(
        max_workers=effective_max_workers,
        thread_name_prefix="sandbox-create",
    ) as pool:
        for index in range(attempts):
            scheduled = run_start + index / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            scheduled_utc = datetime.fromtimestamp(
                utc_start + index / rate, tz=timezone.utc
            ).isoformat()
            futures.append(
                pool.submit(
                    one_trial,
                    settings=settings,
                    provider=provider,
                    storage=storage,
                    rate=rate,
                    duration_seconds=duration_seconds,
                    configured_max_workers=max_workers,
                    effective_max_workers=effective_max_workers,
                    trial_index=index + 1,
                    scheduled_monotonic=scheduled,
                    scheduled_at_utc=scheduled_utc,
                )
            )

        results = []
        completed = 0
        for future in as_completed(futures):
            results.append(future.result())
            completed += 1
            if completed == attempts or completed % max(1, attempts // 10) == 0:
                with PRINT_LOCK:
                    print(
                        f"  进度 {completed}/{attempts}",
                        flush=True,
                    )
    return sorted(results, key=lambda row: row.trial_index)


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return round(ordered[index], 3)


def summarize(results: list[TrialResult]) -> list[dict]:
    groups: dict[tuple[str, str, int, float, int, int], list[TrialResult]] = {}
    for row in results:
        groups.setdefault(
            (
                row.provider,
                row.storage,
                row.target_rate_per_s,
                row.duration_seconds,
                row.configured_max_workers,
                row.effective_max_workers,
            ),
            [],
        ).append(row)

    summary = []
    for (
        provider,
        storage,
        rate,
        duration_seconds,
        configured_max_workers,
        effective_max_workers,
    ), rows in sorted(groups.items()):
        api = [row.api_latency_ms for row in rows if row.api_latency_ms is not None]
        command = [
            row.first_command_latency_ms
            for row in rows
            if row.first_command_latency_ms is not None
        ]
        second_command = [
            row.second_command_latency_ms
            for row in rows
            if row.second_command_latency_ms is not None
        ]
        queue = [row.queue_delay_ms for row in rows]
        attempts = len(rows)
        summary.append(
            {
                "provider": provider,
                "storage": storage,
                "target_rate_per_s": rate,
                "rate_label": f"{rate}tps",
                "duration_seconds": duration_seconds,
                "configured_max_workers": configured_max_workers,
                "effective_max_workers": effective_max_workers,
                "attempts": attempts,
                "api_success_rate_pct": round(
                    100 * sum(row.api_success for row in rows) / attempts, 3
                ),
                "ready_success_rate_pct": round(
                    100 * sum(row.ready_success for row in rows) / attempts, 3
                ),
                "cleanup_success_rate_pct": round(
                    100 * sum(row.cleanup_success for row in rows) / attempts, 3
                ),
                "api_latency_min_ms": round(min(api), 3) if api else None,
                "api_latency_max_ms": round(max(api), 3) if api else None,
                "api_latency_mean_ms": round(statistics.fmean(api), 3)
                if api
                else None,
                "api_latency_p50_ms": percentile(api, 50),
                "api_latency_p90_ms": percentile(api, 90),
                "api_latency_p95_ms": percentile(api, 95),
                "api_latency_p99_ms": percentile(api, 99),
                "first_command_latency_min_ms": round(min(command), 3)
                if command
                else None,
                "first_command_latency_max_ms": round(max(command), 3)
                if command
                else None,
                "first_command_latency_mean_ms": round(
                    statistics.fmean(command), 3
                )
                if command
                else None,
                "first_command_latency_p50_ms": percentile(command, 50),
                "first_command_latency_p90_ms": percentile(command, 90),
                "first_command_latency_p95_ms": percentile(command, 95),
                "first_command_latency_p99_ms": percentile(command, 99),
                "second_command_latency_min_ms": round(min(second_command), 3)
                if second_command
                else None,
                "second_command_latency_max_ms": round(max(second_command), 3)
                if second_command
                else None,
                "second_command_latency_mean_ms": round(
                    statistics.fmean(second_command), 3
                )
                if second_command
                else None,
                "second_command_latency_p50_ms": percentile(second_command, 50),
                "second_command_latency_p90_ms": percentile(second_command, 90),
                "second_command_latency_p95_ms": percentile(second_command, 95),
                "second_command_latency_p99_ms": percentile(second_command, 99),
                "schedule_delay_p95_ms": percentile(queue, 95),
            }
        )
    return summary


def write_csv(
    path: Path,
    rows: list[dict],
    fieldnames: list[str] | None = None,
) -> None:
    if not rows and not fieldnames:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames or list(rows[0]),
        )
        writer.writeheader()
        writer.writerows(rows)


def append_global_summary(
    path: Path,
    rows: list[dict],
) -> None:
    """Append result rows while preserving any columns added by future versions."""
    existing_rows: list[dict] = []
    fieldnames: list[str] = []
    if path.exists():
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames.extend(reader.fieldnames or [])
            existing_rows.extend(reader)

    for row in rows:
        for name in row:
            if name not in fieldnames:
                fieldnames.append(name)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    write_csv(temp_path, existing_rows + rows, fieldnames=fieldnames)
    temp_path.replace(path)


GLOBAL_HISTORY_DIMENSIONS = {
    "run_id",
    "test_name",
    "completed_at_local",
    "result_directory",
    "provider",
    "storage",
    "target_rate_per_s",
    "rate_label",
    "duration_seconds",
}


def metric_unit(metric: str) -> str:
    if metric.endswith("_ms"):
        return "ms"
    if metric.endswith("_pct"):
        return "%"
    if metric in {"attempts", "configured_max_workers", "effective_max_workers"}:
        return "count"
    return ""


def refresh_global_matrix(history_path: Path, matrix_path: Path) -> None:
    """Rebuild a latest-value vendor/rate matrix from the append-only history."""
    if not history_path.exists():
        return

    with history_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        history_rows = list(reader)
        history_fields = reader.fieldnames or []

    metric_fields = [
        field for field in history_fields if field not in GLOBAL_HISTORY_DIMENSIONS
    ]
    rates = sorted(
        {
            int(float(row["target_rate_per_s"]))
            for row in history_rows
            if row.get("target_rate_per_s", "").strip()
        }
    )
    rate_columns = [f"{rate}tps" for rate in rates]

    # History is append-only, so later rows intentionally replace earlier cells.
    matrix: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for row in history_rows:
        raw_rate = row.get("target_rate_per_s", "").strip()
        if not raw_rate:
            continue
        rate_column = f"{int(float(raw_rate))}tps"
        for metric in metric_fields:
            value = row.get(metric, "")
            if value in (None, ""):
                continue
            key = (
                row.get("test_name", ""),
                row.get("provider", ""),
                row.get("storage", ""),
                row.get("duration_seconds", ""),
                metric,
            )
            matrix.setdefault(key, {})[rate_column] = value

    metric_order = {name: index for index, name in enumerate(metric_fields)}

    def duration_sort_value(value: str) -> tuple[int, float | str]:
        try:
            return (0, float(value))
        except ValueError:
            return (1, value)

    matrix_rows = []
    sorted_keys = sorted(
        matrix,
        key=lambda key: (
            key[0],
            key[2],
            duration_sort_value(key[3]),
            metric_order.get(key[4], len(metric_order)),
            key[1],
        ),
    )
    for test_name, provider, storage, duration_seconds, metric in sorted_keys:
        values = matrix[(test_name, provider, storage, duration_seconds, metric)]
        matrix_rows.append(
            {
                "test_name": test_name,
                "provider": provider,
                "storage": storage,
                "duration_seconds": duration_seconds,
                "metric": metric,
                "unit": metric_unit(metric),
                **{column: values.get(column, "") for column in rate_columns},
            }
        )

    fieldnames = [
        "test_name",
        "provider",
        "storage",
        "duration_seconds",
        "metric",
        "unit",
        *rate_columns,
    ]
    temp_path = matrix_path.with_suffix(matrix_path.suffix + ".tmp")
    write_csv(temp_path, matrix_rows, fieldnames=fieldnames)
    temp_path.replace(matrix_path)


def write_failure_logs(
    output_dir: Path,
    title: str,
    results: list[TrialResult],
) -> None:
    failures = [
        row
        for row in results
        if not row.api_success or not row.ready_success or not row.cleanup_success
    ]
    fieldnames = list(asdict(results[0])) if results else []
    write_csv(
        output_dir / f"{title}_失败日志.csv",
        [asdict(row) for row in failures],
        fieldnames=fieldnames,
    )

    lines = [
        f"测试名称: {title}",
        f"总请求数: {len(results)}",
        f"失败请求数: {len(failures)}",
        "",
    ]
    if not failures:
        lines.append("本轮无失败。")
    else:
        for row in failures:
            lines.extend(
                [
                    "=" * 80,
                    f"trial_index: {row.trial_index}",
                    f"scheduled_at_utc: {row.scheduled_at_utc}",
                    f"sandbox_id: {row.sandbox_id or '(未分配)'}",
                    f"api_success: {row.api_success}",
                    f"ready_success: {row.ready_success}",
                    f"cleanup_success: {row.cleanup_success}",
                    f"failure_phase: {row.failure_phase or '(无主流程错误)'}",
                    f"error_type: {row.error_type or '(无)'}",
                    f"error_message: {row.error_message or '(无)'}",
                ]
            )
            if row.error_traceback:
                lines.extend(["error_traceback:", row.error_traceback.rstrip()])
            if not row.cleanup_success:
                lines.extend(
                    [
                        f"cleanup_error_type: {row.cleanup_error_type or '(无)'}",
                        (
                            "cleanup_error_message: "
                            f"{row.cleanup_error_message or '(无)'}"
                        ),
                    ]
                )
                if row.cleanup_error_traceback:
                    lines.extend(
                        [
                            "cleanup_error_traceback:",
                            row.cleanup_error_traceback.rstrip(),
                        ]
                    )
            lines.append("")
    (output_dir / f"{title}_失败日志.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def result_title(results: list[TrialResult], test_name: str) -> str:
    storage_names = {
        "none": "无挂载",
        "oss": "对象存储OSS",
        "file": "文件存储NAS",
    }
    storages = list(dict.fromkeys(row.storage for row in results))
    rates = sorted({row.target_rate_per_s for row in results})
    durations = sorted({row.duration_seconds for row in results})
    storage_part = "-".join(storage_names.get(item, item) for item in storages)
    rate_part = "-".join(str(rate) for rate in rates) + "tps"
    duration_part = "-".join(
        str(int(value)) if float(value).is_integer() else f"{value:g}"
        for value in durations
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return f"{test_name}_{storage_part}_{rate_part}_持续{duration_part}s_{stamp}"


def save_results(
    results: list[TrialResult],
    output_root: Path,
    test_name: str,
    add_to_global: bool = False,
) -> Path:
    if not results:
        raise RuntimeError("没有可保存的测试结果")
    providers = {row.provider for row in results}
    if len(providers) != 1:
        raise RuntimeError("同一结果目录只能保存一个厂商的测试结果")
    provider = next(iter(providers))
    title = result_title(results, test_name)
    output_dir = output_root / provider / title
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_rows = [asdict(row) for row in results]
    summary_rows = summarize(results)
    write_csv(output_dir / f"{title}_原始明细.csv", raw_rows)
    write_csv(output_dir / f"{title}_汇总.csv", summary_rows)
    write_failure_logs(output_dir, title, results)
    (output_dir / f"{title}_汇总.json").write_text(
        json.dumps(summary_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if add_to_global:
        completed_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        global_rows = [
            {
                "run_id": title,
                "test_name": test_name,
                "completed_at_local": completed_at,
                "result_directory": str(Path(provider) / title),
                **row,
            }
            for row in summary_rows
        ]
        history_path = output_root / "全局测试历史.csv"
        append_global_summary(history_path, global_rows)
        refresh_global_matrix(history_path, output_root / "全局测试结果.csv")
    return output_dir


def parse_csv_ints(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("并发档位必须是逗号分隔的正整数") from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("并发档位必须是逗号分隔的正整数")
    return values


def parse_storages(value: str) -> list[str]:
    values = [item.strip().lower() for item in value.split(",") if item.strip()]
    invalid = sorted(set(values) - set(STORAGE_CHOICES))
    if not values or invalid:
        raise argparse.ArgumentTypeError(
            f"存储类型只支持 {','.join(STORAGE_CHOICES)}；无效值: {','.join(invalid)}"
        )
    return list(dict.fromkeys(values))


def parse_provider(value: str) -> str:
    provider = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", provider):
        raise argparse.ArgumentTypeError(
            "厂商标识只允许小写字母、数字、下划线和短横线，最长 64 个字符"
        )
    return provider


def attempts_for(rates: list[int], duration: float, storages: list[str]) -> int:
    return sum(max(1, math.ceil(rate * duration)) for rate in rates) * len(storages)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="阿里云 FC 云沙箱并发启动速度测试"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke = subparsers.add_parser("smoke", help="只创建 1 个沙箱做连通性验证")
    smoke.add_argument(
        "--storage", choices=STORAGE_CHOICES, default="none", help="存储场景"
    )
    smoke.add_argument(
        "--provider", type=parse_provider, default=DEFAULT_PROVIDER, help="厂商标识"
    )
    smoke.add_argument("--output", type=Path, default=Path("results"))

    plan = subparsers.add_parser("plan", help="只计算测试规模，不调用云端")
    run = subparsers.add_parser("run", help="执行正式并发测试")
    for target in (plan, run):
        target.add_argument(
            "--provider",
            type=parse_provider,
            default=DEFAULT_PROVIDER,
            help=f"厂商标识，默认 {DEFAULT_PROVIDER}",
        )
        target.add_argument(
            "--rates",
            type=parse_csv_ints,
            default=[1, 10, 50, 100, 200],
            help="每秒发起创建数，默认 1,10,50,100,200",
        )
        target.add_argument(
            "--duration-seconds",
            type=float,
            default=1.0,
            help="每个档位发压持续秒数，默认 1",
        )
        target.add_argument(
            "--storages",
            type=parse_storages,
            default=["none"],
            help="none,oss,file 的任意组合，默认 none",
        )
    run.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"本地创建线程上限，默认 {DEFAULT_MAX_WORKERS}",
    )
    run.add_argument("--output", type=Path, default=Path("results"))
    run.add_argument(
        "--confirm",
        action="store_true",
        help="确认本次调用会创建可计费云资源",
    )
    return parser


def print_plan(
    provider: str,
    rates: list[int],
    duration: float,
    storages: list[str],
) -> None:
    print(f"厂商标识: {provider}")
    print(f"存储场景: {', '.join(storages)}")
    print(f"并发档位: {', '.join(str(rate) + '/s' for rate in rates)}")
    print(f"每档持续: {duration:g} 秒")
    for storage in storages:
        for rate in rates:
            print(
                f"  {storage:>4} @ {rate:>3}/s: "
                f"{max(1, math.ceil(rate * duration))} 次创建"
            )
    print(f"预计 Sandbox.create 总次数: {attempts_for(rates, duration, storages)}")


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "plan":
        if args.duration_seconds <= 0:
            raise RuntimeError("--duration-seconds 必须大于 0")
        print_plan(args.provider, args.rates, args.duration_seconds, args.storages)
        return 0

    settings = load_settings(require_credentials=True)
    if args.command == "smoke":
        metadata_for(args.storage, settings)
        print(f"开始 smoke 测试，存储场景={args.storage}")
        result = one_trial(
            settings=settings,
            provider=args.provider,
            storage=args.storage,
            rate=1,
            duration_seconds=1.0,
            configured_max_workers=1,
            effective_max_workers=1,
            trial_index=1,
            scheduled_monotonic=time.perf_counter(),
            scheduled_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        output_dir = save_results([result], args.output, "连通性测试")
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        print(f"结果目录: {output_dir.resolve()}")
        return 0 if result.ready_success and result.cleanup_success else 1

    if args.duration_seconds <= 0:
        raise RuntimeError("--duration-seconds 必须大于 0")
    if args.max_workers <= 0:
        raise RuntimeError("--max-workers 必须大于 0")
    for storage in args.storages:
        metadata_for(storage, settings)
    print_plan(args.provider, args.rates, args.duration_seconds, args.storages)
    if not args.confirm:
        print("\n未执行：正式测试请在命令末尾加 --confirm。")
        return 2

    all_results: list[TrialResult] = []
    for storage in args.storages:
        for rate in args.rates:
            print(f"\n开始: storage={storage}, rate={rate}/s")
            all_results.extend(
                run_rate(
                    settings=settings,
                    provider=args.provider,
                    storage=storage,
                    rate=rate,
                    duration_seconds=args.duration_seconds,
                    max_workers=args.max_workers,
                )
            )
    output_dir = save_results(
        all_results,
        args.output,
        "启动并发速度",
        add_to_global=True,
    )
    print(f"\n完成。结果目录: {output_dir.resolve()}")
    print(json.dumps(summarize(all_results), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n用户中断。已提交任务会在 finally 中尝试释放沙箱。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        if os.environ.get("DEBUG", "").lower() in {"1", "true", "yes"}:
            traceback.print_exc()
        raise SystemExit(1)
