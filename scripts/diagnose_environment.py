from __future__ import annotations

from launch_portscope import inspect_environment, print_report, project_root_from_script, runtime_paths


def main() -> int:
    paths = runtime_paths(project_root_from_script())
    report = inspect_environment(paths, preferred_port=8501, deep_advanced_check=True)
    print_report(report, detailed=True)
    if not report.core_ready:
        print("[PortScope] 推荐操作：运行项目根目录 repair_environment.bat。")
        return 2
    if report.warnings:
        print("[PortScope] 推荐操作：基础启动可用；请按上方警告决定是否处理高级功能。")
    else:
        print("[PortScope] 推荐操作：可直接运行 run.bat。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
