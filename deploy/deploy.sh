#!/usr/bin/env bash
# grantmatchpy 배포 (개발 PC → 서버, 계정 grantmatchpy). 코드는 ~/app, 직전 코드는 ~/app.prev 로 한 벌만 남겨 되돌릴 수 있게 한다.
#   deploy/deploy.sh <계정>@<서버> [SSH 키]            (저장소 grantmatchpy/ 에서)
#   deploy/deploy.sh <계정>@<서버> [SSH 키] rollback   직전 코드로 되돌리기
# 순서: app 묶기 → 올리기 → ~/app.new 에 풀기 → 가상환경(~/venv)에 의존성 설치 → app 교체 → 세 서비스 다시 시작 → 상태 확인 → 실패하면 app.prev 로 되돌림
# 가상환경은 코드와 따로 ~/venv 에 둔다(모델 런타임 같은 큰 패키지를 배포마다 다시 받지 않게). 서비스 파일은 ExecStart 가 ~/app/.venv 를 보므로 ~/app/.venv → ~/venv 링크를 둔다.
set -euo pipefail
TARGET="${1:?계정@서버 를 넣어 주세요}"
KEY="${2:-}"
MODE="${3:-deploy}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "${KEY}" ] && SSH_OPTS+=(-i "${KEY}")

if [ "${MODE}" = "rollback" ]; then
ssh "${SSH_OPTS[@]}" "${TARGET}" bash -s <<'REMOTE'
set -euo pipefail
cd ~
[ -d app.prev ] || { echo "되돌릴 코드(app.prev)가 없습니다"; exit 1; }
rm -rf app.bad && mv app app.bad && mv app.prev app && ln -sfn ~/venv app/.venv
sudo /usr/bin/systemctl restart grantmatchpy grantmatchpy-attach grantmatchpy-embed
echo "되돌림 완료(문제 코드는 ~/app.bad 에 남겨 둠)"
REMOTE
exit 0
fi

cd "$(dirname "$0")/.."
STAMP="$(date -u +%Y%m%d-%H%M%S)"
ARCHIVE="/tmp/grantmatchpy-${STAMP}.tar.gz"
tar -czf "${ARCHIVE}" --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='tests' app requirements.txt pyproject.toml
echo "== 올리기: ${ARCHIVE}"
scp "${SSH_OPTS[@]}" "${ARCHIVE}" "${TARGET}:grantmatchpy-${STAMP}.tar.gz"

ssh "${SSH_OPTS[@]}" "${TARGET}" bash -s -- "${STAMP}" <<'REMOTE'
set -euo pipefail
STAMP="$1"; cd ~
rm -rf app.new && mkdir app.new && tar -xzf "grantmatchpy-${STAMP}.tar.gz" -C app.new
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r app.new/requirements.txt || { echo "!! 의존성 설치 실패 — 코드는 그대로"; rm -rf app.new; exit 1; }
rm -rf app.prev
[ -d app ] && mv app app.prev
mv app.new app && ln -sfn ~/venv app/.venv
sudo /usr/bin/systemctl restart grantmatchpy grantmatchpy-attach grantmatchpy-embed
ok=0
for i in 1 2 3 4 5 6; do
  a="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8310/internal/health || true)"
  b="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8311/internal/health || true)"
  [ "$a" = "200" ] && [ "$b" = "200" ] && { ok=1; break; }
  sleep 3
done
if [ "$ok" != "1" ]; then
  echo "!! 상태 확인 실패(api=$a, attach=$b) — 직전 코드로 되돌립니다"
  if [ -d app.prev ]; then rm -rf app.bad && mv app app.bad && mv app.prev app && ln -sfn ~/venv app/.venv; sudo /usr/bin/systemctl restart grantmatchpy grantmatchpy-attach grantmatchpy-embed; fi
  exit 1
fi
rm -f "grantmatchpy-${STAMP}.tar.gz"
echo "배포 완료"
REMOTE
rm -f "${ARCHIVE}"
