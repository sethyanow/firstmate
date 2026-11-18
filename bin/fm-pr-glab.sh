#!/usr/bin/env bash
set -eu

host=$1
shift
credential_host=${GITLAB_HOST:-${GITLAB_URI:-${GL_HOST:-${GITLAB_URL:-}}}}
credential_host=${credential_host#https://}
credential_host=${credential_host#http://}
credential_host=${credential_host%/}
if [ -z "$credential_host" ]; then
  case ${GITLAB_REPO:-} in
    https://*/*|http://*/*)
      credential_host=${GITLAB_REPO#*://}
      credential_host=${credential_host%%/*}
      ;;
  esac
fi
if [ "$credential_host" != "$host" ]; then
  unset GITLAB_TOKEN GITLAB_ACCESS_TOKEN OAUTH_TOKEN GITLAB_API_HOST GLAB_API_PROTOCOL
fi
if [ "${CI_SERVER_FQDN:-}" != "$host" ]; then
  unset GITLAB_CI CI_JOB_TOKEN
fi
unset GITLAB_REPO
export GITLAB_HOST=$host
exec glab "$@"
