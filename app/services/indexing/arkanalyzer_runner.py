import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.config import (
    ARKANALYZER_CMD,
    ARKANALYZER_ENTRY,
    ARKANALYZER_ROOT,
    ARKANALYZER_TIMEOUT_SEC,
)


@dataclass
class ParserRunResult:
    parser_mode: str
    manifest: dict
    logs: list[str] = field(default_factory=list)
    setup_hints: list[str] = field(default_factory=list)


class ArkAnalyzerRunner:
    def run(self, repo_path: Path, artifact_dir: Path) -> ParserRunResult:
        output_dir = artifact_dir / 'arkanalyzer-output'
        output_dir.mkdir(parents=True, exist_ok=True)

        if ARKANALYZER_CMD:
            command = ARKANALYZER_CMD.format(
                repo_path=str(repo_path),
                output_dir=str(output_dir),
                output_path=str(artifact_dir / 'arkanalyzer-manifest.json'),
            )
            return self._run_command(
                command=command,
                shell=True,
                cwd=None,
                output_dir=output_dir,
                source='env',
            )

        resolved = self._resolve_default_command(repo_path, output_dir)
        if resolved is None:
            raise RuntimeError('\n'.join(self.build_setup_hints()))

        command, cwd = resolved
        return self._run_command(
            command=command,
            shell=False,
            cwd=cwd,
            output_dir=output_dir,
            source='local',
        )

    def build_setup_hints(self, error_message: str = '') -> list[str]:
        hints: list[str] = []
        if not ARKANALYZER_ROOT.exists():
            hints.append(f'未检测到 ArkAnalyzer 目录：{ARKANALYZER_ROOT}')
        if shutil.which('node') is None:
            hints.append('未检测到 node 命令，请先安装 Node.js 并确认 node/npm 已加入 PATH。')
        if ARKANALYZER_ROOT.exists() and not (ARKANALYZER_ROOT / 'node_modules').exists():
            hints.append('ArkAnalyzer 尚未安装依赖，请在 arkanalyzer 目录执行 npm install。')
        if ARKANALYZER_ROOT.exists() and not self._find_entry().exists():
            hints.append('ArkAnalyzer 尚未生成可执行入口，请先执行 npm run build。后端同时兼容 out 和 lib 两种构建产物。')
        if error_message:
            hints.append(f'最近一次 ArkAnalyzer 错误输出：{error_message.strip()[:300]}')
        if not hints:
            hints.append('ArkAnalyzer 未配置成功，请检查 ARKANALYZER_CMD 或本地安装状态。')
        return hints

    def _resolve_default_command(self, repo_path: Path, output_dir: Path) -> tuple[list[str], Path] | None:
        if not ARKANALYZER_ROOT.exists():
            return None
        if shutil.which('node') is None:
            return None
        if not (ARKANALYZER_ROOT / 'node_modules').exists():
            return None

        entry = self._find_entry()
        if not entry.exists():
            return None

        return (
            [
                'node',
                str(entry),
                str(repo_path),
                str(output_dir),
                '--project',
                '--infer-types',
                '1',
            ],
            ARKANALYZER_ROOT,
        )

    def _find_entry(self) -> Path:
        candidates = [
            ARKANALYZER_ROOT / ARKANALYZER_ENTRY,
            ARKANALYZER_ROOT / 'out/src/save/serializeArkIR.js',
            ARKANALYZER_ROOT / 'lib/save/serializeArkIR.js',
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _run_command(
        self,
        command: str | list[str],
        shell: bool,
        cwd: Path | None,
        output_dir: Path,
        source: str,
    ) -> ParserRunResult:
        display_command = subprocess.list2cmdline(command) if isinstance(command, list) else command

        try:
            completed = subprocess.run(
                command,
                shell=shell,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=ARKANALYZER_TIMEOUT_SEC,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('\n'.join(self.build_setup_hints(exc.stderr or exc.stdout or str(exc)))) from exc

        output_files = sorted(path.relative_to(output_dir).as_posix() for path in output_dir.rglob('*.json'))
        if not output_files:
            raise RuntimeError('ArkAnalyzer 执行完成，但未生成任何 JSON 输出文件。')

        manifest = {
            'parser_mode': 'arkanalyzer',
            'source': source,
            'command': display_command,
            'cwd': str(cwd) if cwd else '',
            'output_dir': str(output_dir),
            'output_files': output_files,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
        }
        return ParserRunResult(
            parser_mode='arkanalyzer',
            manifest=manifest,
            logs=[f'ArkAnalyzer 已执行，输出文件 {len(output_files)} 个。'],
        )
