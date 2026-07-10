#!/bin/bash
# 부가서비스 대시보드 자동 갱신
# BQ 재집계 → data.js 생성 → 검증 → 변경 있으면 커밋/푸시
# launchd가 매일 11:00, 15:00(KST)에 호출. 11시 성공하면 15시는 마커로 스킵.

REPO="/Users/admin/upselling-work/dashboards/upselling-dashboard-addon"
LOG="$REPO/scripts/refresh.log"
MARKER="$REPO/scripts/.last_success"
TODAY=$(date +%Y-%m-%d)

# launchd 환경 PATH 보정
export PATH="/opt/homebrew/bin:/usr/local/bin:/Users/admin/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export HOME="/Users/admin"
# BQ 인증: gcloud 사용자 계정이 비어도 ADC 토큰으로 bq 실행 (auth 만료 방어)
export CLOUDSDK_AUTH_ACCESS_TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)

cd "$REPO" || exit 1

# 오늘 이미 성공했으면 스킵 (15시 재시도 방어)
if [ "$(cat "$MARKER" 2>/dev/null)" = "$TODAY" ]; then
  echo "$(date '+%F %T') 오늘 이미 갱신됨, 스킵" >> "$LOG"
  exit 0
fi

{
  echo "=== $(date '+%F %T') 갱신 시작 ==="

  # 1) 데이터 생성 (cut = 어제 KST, 기본값)
  if ! python3 scripts/gen_data.py --out data.js; then
    echo "gen_data.py 실패 → 종료 (재시도 대기)"; exit 1
  fi

  # 2) 검증: data.js 파싱 + 핵심 값 존재
  if ! node -e "global.window={};require('./data.js');const D=window.DASH;if(!(D.jeju.rsv>0)||!D.meta.cut||!(D.total.rev>0))process.exit(1);console.log('검증 OK 제주'+D.jeju.rsv+'예약 cut'+D.meta.cut)"; then
    echo "검증 실패 → 종료 (커밋 안 함, 재시도 대기)"; exit 1
  fi

  # 3) 변경 있으면 커밋/푸시
  if git diff --quiet data.js; then
    echo "data.js 변경 없음"
  else
    git add data.js
    git commit -q -m "데이터 자동 갱신 (${TODAY})" || { echo "commit 실패"; exit 1; }
    if git push -q origin HEAD; then echo "푸시 완료"; else echo "푸시 실패 → 재시도 대기"; exit 1; fi
  fi

  # 성공 마커
  echo "$TODAY" > "$MARKER"
  echo "=== $(date '+%F %T') 성공 ==="
} >> "$LOG" 2>&1
