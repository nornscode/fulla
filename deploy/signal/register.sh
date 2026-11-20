#!/bin/sh
# Register Fulla's Signal number, once. Run with `fly proxy 8080:8080 -a
# fulla-signal` open in another terminal, so localhost:8080 is the container.
#
#   register.sh +16475550100 [captcha-token] [--voice]
#   register.sh +16475550100 verify 123456
#
# Signal almost always asks for a captcha: open
# https://signalcaptchas.org/registration/generate, solve it, right-click
# "Open Signal", copy the link, and pass the whole thing (it starts with
# signalcaptcha://). Tokens expire within minutes, so do it right before.
# Numbers that cannot take SMS (VoIP, landlines) get the code by voice
# call with --voice; Signal insists an SMS attempt comes first. After
# verifying, restart the machine so the daemon picks the account up.
set -eu

api=${SIGNAL_API:-http://127.0.0.1:8080}
number=${1:?number in E.164, like +16475550100}
shift

if [ "${1:-}" = "verify" ]; then
    code=${2:?verification code}
    curl -fsS -X POST -H 'Content-Type: application/json' \
        "$api/v1/register/$number/verify/$code" -d '{}'
    echo "verified. now: fly machine restart -a fulla-signal, then set a pin:"
    echo "  curl -X POST -H 'Content-Type: application/json' $api/v1/accounts/$number/pin -d '{\"pin\":\"<digits>\"}'"
    exit 0
fi

captcha=""
voice=false
for arg in "$@"; do
    case "$arg" in
        --voice) voice=true ;;
        signalcaptcha://*) captcha=${arg#signalcaptcha://} ;;
        *) captcha=$arg ;;
    esac
done

body=$(printf '{"use_voice": %s' "$voice")
if [ -n "$captcha" ]; then
    body=$(printf '%s, "captcha": "%s"' "$body" "$captcha")
fi
body="$body}"

curl -fsS -X POST -H 'Content-Type: application/json' "$api/v1/register/$number" -d "$body" && echo
echo "code requested. then: register.sh $number verify <code>"
