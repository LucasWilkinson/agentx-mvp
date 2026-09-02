#!/usr/bin/env bash
# Source after .env.b200: source scripts/b200-glm53-config.sh <config>.

case "${1:-}" in
  pcp8-ep8-mtp3)
    MANIFESTO_USER="${B200_OWNER_PREFIX:-lwilkinson}-glm53-pcp8-ep8"
    MANIFESTO_SPEC=glm-5.3/b200/p1-pcp8ep-d1-dp8ep-mtp-agentx
    MODEL=glm53-b200-pcp8-ep8
    ROUTER_RELEASE="${B200_ROUTER_PREFIX:-lw}-glm53-pcp8-ep8-router"
    ROUTER_PROBE_PORT=33101
    ;;
  pcp8-dcp8-ep8-a2a-mtp3)
    MANIFESTO_USER="${B200_OWNER_PREFIX:-lwilkinson}-glm53-pcp8-dcp8-ep8-a2a"
    MANIFESTO_SPEC=glm-5.3/b200/p1-pcp8dcp8ep-d1-dp8ep-mtp-a2a-agentx
    MODEL=glm53-b200-pcp8-dcp8-ep8-a2a
    ROUTER_RELEASE="${B200_ROUTER_PREFIX:-lw}-glm53-pcp8-dcp8-a2a-router"
    ROUTER_PROBE_PORT=33102
    ;;
  tp8-ep8-mtp3)
    MANIFESTO_USER="${B200_OWNER_PREFIX:-lwilkinson}-glm53-tp8-ep8"
    MANIFESTO_SPEC=glm-5.3/b200/p1-tp8ep-d1-dp8ep-mtp-agentx
    MODEL=glm53-b200-tp8-ep8
    ROUTER_RELEASE="${B200_ROUTER_PREFIX:-lw}-glm53-tp8-ep8-router"
    ROUTER_PROBE_PORT=33103
    ;;
  *)
    echo "usage: source scripts/b200-glm53-config.sh <pcp8-ep8-mtp3|pcp8-dcp8-ep8-a2a-mtp3|tp8-ep8-mtp3>" >&2
    return 2 2>/dev/null || exit 2
    ;;
esac

URL="http://${ROUTER_RELEASE}-epp:80"
export MANIFESTO_USER MANIFESTO_SPEC MODEL ROUTER_RELEASE ROUTER_PROBE_PORT URL
