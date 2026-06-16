# plan-finder

> **WARNING**: This project was built entirely through vibe coding. The author takes absolutely no responsibility for the results.

Claude AI를 반복 실행하여 코드베이스의 개선점을 자동으로 발견하는 CLI 도구.

코드 품질, 버그, 리팩토링, 성능, 보안 등 모든 종류의 개선점을 찾아서 마크다운 리포트로 저장한다.

## 설치

```bash
cd ~/plan-finder
uv sync
```

### 사전 요구사항

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) 패키지 매니저
- 백엔드 중 하나 (기본은 Claude):
  - **Claude** (`--backend claude`, 기본): [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) 인증 완료 (claude-agent-sdk가 사용)
  - **Codex** (`--backend codex`): [Codex CLI](https://developers.openai.com/codex/cli) 설치 + `codex login` 완료
- [ccusage](https://github.com/ryoppippi/ccusage) — Claude 백엔드의 세션 비용 자동 감지에 사용 (`brew install ccusage`)

## 백엔드 선택 (Claude / Codex)

`--backend` 옵션으로 분석에 사용할 AI를 고른다. 기본값은 `claude`다.

```bash
# Claude (기본) — claude-agent-sdk 사용
uv run --project ~/plan-finder plan-finder --prompt "..." --max 50

# Codex — codex CLI 사용 (먼저 `codex login` 필요)
uv run --project ~/plan-finder plan-finder --backend codex --prompt "..." --max 50
```

Codex 백엔드 동작 방식:

- `codex exec`를 **read-only 샌드박스**로 실행한다. 파일을 수정할 수 없다.
- 모델·추론 강도(reasoning effort) 등은 사용자의 `~/.codex/config.toml`을 그대로 따른다. `--model gpt-5.5` 처럼 덮어쓸 수 있다.
- 반복 간 세션 유지는 `codex exec resume`로 처리한다 (`--no-resume`으로 비활성화).
- **비용($) 기반 쓰로틀은 사용하지 않는다.** Codex는 구독제라 사용량 한도에 도달하면 에러 메시지의 리셋 시각(`try again at 7:45 PM` 등)을 파싱해 그 시각까지 자동 대기한다.

## 빠른 시작

개선점을 찾고 싶은 프로젝트 디렉토리에서 실행한다.

### 프롬프트 지정 방법

**직접 입력:**

```bash
cd ~/my-project
uv run --project ~/plan-finder plan-finder \
  --prompt "코드베이스에서 임의의 개선점을 찾아서 제안해줘. 코드 품질, 버그, 리팩토링, 성능 등 어떤 종류도 좋다." \
  --max 50
```

**preset 사용:**

미리 정의된 preset이 있으면 이름으로 바로 적용할 수 있다. 이름이 정확히 일치해야 하며, 없으면 에러가 발생한다.

```bash
# 사용 가능한 preset 목록 확인
uv run --project ~/plan-finder plan-finder --preset ?

# preset 적용
uv run --project ~/plan-finder plan-finder --preset unity --max 50
```

**대화형 입력 (프롬프트/preset 미지정 시):**

`--prompt`와 `--preset`을 모두 생략하면 프로젝트 타입과 집중 영역을 대화형으로 입력받아 프롬프트를 생성한다.

### 대화형으로 직접 검토하기

```bash
cd ~/my-project
uv run --project ~/plan-finder plan-finder \
  --prompt "코드베이스에서 임의의 개선점을 찾아서 제안해줘. 코드 품질, 버그, 리팩토링, 성능 등 어떤 종류도 좋다." \
  --max 50
```

Claude가 코드베이스를 분석해서 개선점을 하나씩 찾아 보여준다. 각 plan에 대해:

- **y (승인)**: `~/claude-reports/{프로젝트명}/`에 마크다운 파일로 저장
- **n (거절)**: 거절 사유를 남기고, 다음 반복에서 같은 제안을 하지 않음
- **r (수정 요청)**: 피드백을 입력하면 Claude가 같은 세션에서 plan을 수정. 수정된 plan에 대해 다시 y/n/r 선택 가능

`Ctrl+C`로 언제든 중단 가능.

### 자는 동안 자동으로 돌리기

데몬으로 새벽 3시에 시작, 7시 30분에 자동 종료:

```bash
cd ~/my-project

# 데몬 시작
~/plan-finder/plan-finder-daemon.sh start --at 03:00 -- \
  --auto \
  --prompt '코드베이스에서 임의의 개선점을 찾아서 제안해줘. 코드 품질, 버그, 리팩토링, 성능 등 어떤 종류도 좋다.' \
  --max 50 \
  --stop-at 07:30

# 상태 확인
~/plan-finder/plan-finder-daemon.sh status

# 중지
~/plan-finder/plan-finder-daemon.sh stop
```

결과는 `~/claude-reports/{프로젝트명}/pending/`에 저장되고, 나중에 사람이 검토한다.

Codex 백엔드로 돌리려면 인자에 `--backend codex`만 추가하면 된다. 데몬은 인자를 그대로 전달한다.

### 대화형 vs 자동 모드 비교

| | 대화형 (기본) | 자동 (--auto) |
|---|---|---|
| 사용자 개입 | 매 plan마다 승인/거절/수정 | 없음 |
| 저장 위치 | `~/claude-reports/{프로젝트}/` | `~/claude-reports/{프로젝트}/pending/` |
| 쓰로틀 | 기본 활성 (`--no-throttle`로 비활성화 가능) | 기본 활성 |
| 용도 | 직접 보면서 검토 | 야간/무인 실행 |

## 쓰로틀링

세션 비용($)을 기준으로 속도를 조절한다. [ccusage](https://github.com/ryoppippi/ccusage)에서 현재 세션의 사용 비용을 자동 감지한다.

- **공식**: `(사용 비용 / 세션 예산) * 1.05 < (경과 시간 / 세션 시간)`
- **기본 예산**: $40 (`--session-budget`으로 조절)
- 세션 전체 비용을 추적하므로 다른 Claude 작업의 사용분도 반영됨
- 매 iteration마다 상태 표시:

```
Cost: $12.50/$40 (31%) | Session: 52% (2.4h left) | 🟢 Plenty (pace 33% vs time 52%) | Model: claude-opus-4-6
```

상태 표시등:
- 🟢 Plenty — 여유 (margin > 15%p)
- 🟡 OK — 괜찮음 (margin > 5%p)
- 🟠 Tight — 빡빡함 (margin > 0)
- 🔴 Over — 초과, 쓰로틀 대기 중

## 쉬는 시간

매일 22:00~03:00 사이에는 쿼리를 보내지 않는다. 이 시간에 iteration이 돌아오면 03:00까지 자동 대기한다.

## 데몬 상세

`plan-finder-daemon.sh`는 현재 터미널의 Claude CLI 인증 환경을 유지한 채 백그라운드로 실행한다.

- 로그: `~/.plan-finder-daemon.log`
- PID: `~/.plan-finder-daemon.pid`
- 인자/타겟 시각/cwd: `~/.plan-finder-daemon.args`, `~/.plan-finder-daemon.target-time`, `~/.plan-finder-daemon.cwd`
- (선택) 사전 훅: `~/.plan-finder-daemon.pre-hook` — [아래 섹션 참고](#사전-훅-pre-hook)

> **참고**: `crontab`은 Claude CLI 인증 환경을 상속받지 못해 동작하지 않는다. 반드시 데몬 스크립트를 사용해야 한다.

### 사전 훅 (pre-hook)

데몬이 매 iteration의 `plan-finder` 실행 직전에 자동으로 호출하는 사용자 정의 훅. 파일이 있으면 `bash`로 실행하고, 없으면 그냥 건너뛴다. 데몬 자체는 프로젝트를 모르고 훅이 알기 때문에, 분석 대상 레포 pull, 시크릿 갱신, 캐시 워밍 등 사이드카 작업을 자유롭게 끼울 수 있다.

```bash
cat > ~/.plan-finder-daemon.pre-hook <<'EOF'
#!/bin/bash
# 예: 분석할 spec/문서 레포를 최신화한 뒤 plan-finder가 깬다
git -C ~/question/cc-spec pull --ff-only --quiet
EOF
chmod +x ~/.plan-finder-daemon.pre-hook
```

특징:

- **실패해도 plan-finder는 계속 실행** — 훅 exit 코드가 0이 아니면 로그에 `pre-hook failed (continuing anyway)`로만 남기고 진행. 네트워크 일시 장애로 새벽 작업이 통째로 날아가지 않게 함.
- **stdout/stderr 전부 `~/.plan-finder-daemon.log`로** 흡수. 별도 로그 파일을 만들고 싶으면 훅 안에서 리다이렉트.
- **PATH는 데몬과 동일** — `~/.local/bin`, Homebrew, Nix 등 데몬이 export한 PATH가 그대로 보임.
- **`$HOME`에 두는 이유**: 다른 데몬 설정 파일(`args`, `target-time`, `cwd`)과 위치를 맞춤. 레포에 커밋되는 파일이 아니므로 `.gitignore` 추가는 불필요.

## 옵션 전체 목록

| 옵션 | 단축 | 설명 | 기본값 |
|---|---|---|---|
| `--prompt` | `-p` | 분석 프롬프트 | (대화형 입력) |
| `--preset` | | Preset 이름 지정. `?`를 입력하면 목록 표시 | 없음 |
| `--backend` | | AI 백엔드 (`claude` 또는 `codex`) | `claude` |
| `--max` | `-m` | 최대 반복 횟수 | 무제한 |
| `--report-dir` | `-d` | 리포트 저장 경로 | `~/claude-reports/{프로젝트명}` |
| `--auto` | | 자동 모드 | 꺼짐 |
| `--no-throttle` | | 쓰로틀링 비활성화 | 꺼짐 (기본 활성) |
| `--session-budget` | | 세션 예산 (USD) | 40.0 |
| `--model` | | 모델 지정 (Claude: `claude-opus-4-6`, Codex: `gpt-5.5` 등) | 백엔드 기본값 |
| `--max-turns` | | Claude 쿼리당 최대 턴 수 | 80 |
| `--stop-at` | | 지정 시각에 종료 (HH:MM) | 없음 |
| `--no-resume` | | 반복 간 Claude 세션 초기화 | 꺼짐 (세션 유지) |
| `--clear-rejections` | | 거절 기록 초기화 후 시작 | |

## 저장 구조

```
~/claude-reports/
└── my-project/
    ├── .state.json                          # 거절/승인/보류 기록 + 통계
    ├── 20260210_143522_fix-null-check.md    # 승인된 plan
    └── pending/
        └── 20260210_030105_refactor-api.md  # 자동 모드에서 저장된 plan (검토 대기)
```

## 동작 원리

1. Claude가 프로젝트 코드를 읽고 개선점 1개를 구조화된 JSON으로 반환
2. 이전에 거절/승인/보류된 plan 목록을 프롬프트에 포함하여 중복 제안 방지
3. 반복 간 Claude 세션을 유지하여 코드베이스 분석 컨텍스트를 재활용 (`--no-resume`으로 비활성화 가능)
4. 쓰로틀은 비용($) 기반: `(비용/예산) * 1.05 < (경과/세션)` — 세션 전체 비용(ccusage)을 기준으로 속도 조절
5. 22:00~03:00 쉬는 시간에는 쿼리를 보내지 않고 자동 대기
6. Rate limit 도달 시 세션 종료까지 자동 대기 후 재시도
7. 분석 중 Claude가 사용하는 도구(Read, Grep 등)를 실시간 표시

## macOS — launchctl로 데몬 관리

macOS에서는 launchd LaunchAgent로 등록해두면 로그인 시 자동 시작 + 죽으면 자동 재시작된다. plist 예시 (`~/Library/LaunchAgents/com.user.planfinder.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.planfinder</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/YOUR_USER/plan-finder/plan-finder-daemon.sh</string>
        <string>_run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOUR_USER</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOUR_USER/.local/bin:/etc/profiles/per-user/YOUR_USER/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/YOUR_USER</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USER/.plan-finder-daemon.out</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USER/.plan-finder-daemon.err</string>
</dict>
</plist>
```

데몬 인자(target time, cwd, prompt 등)는 plist에 박지 않고 한 번 `daemon.sh start --at HH:MM -- ...`로 띄워서 `~/.plan-finder-daemon.{args,target-time,cwd}` 파일에 저장해두면, launchd가 재시작해도 같은 설정으로 다시 돈다.

```bash
# 등록 + 즉시 시작
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.planfinder.plist

# 상태 확인
launchctl list | grep planfinder

# 일시 중지 (재부팅 시 다시 자동 시작됨)
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.user.planfinder.plist

# 영구 비활성 (재부팅에도 살아남음)
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.user.planfinder.plist
launchctl disable gui/$(id -u)/com.user.planfinder

# 다시 활성화
launchctl enable gui/$(id -u)/com.user.planfinder
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.planfinder.plist
```

> **주의**: `KeepAlive=true` 상태에서는 `daemon.sh stop`만으로는 영구히 끌 수 없다. launchd가 즉시 다시 띄우므로 위의 `bootout` + `disable`을 써야 한다.

## 라이선스

MIT
