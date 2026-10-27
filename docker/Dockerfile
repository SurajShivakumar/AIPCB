# KiCad 9 + the board tools' Python deps, for running the autopcb pipeline headless (nothing on the desktop).
#   docker build -t autopcb-kicad docker
# autopcb_run --docker uses it: the repo is mounted at /work and tools/make_esc_v2.py runs inside.
FROM kicad/kicad:9.0.6

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip git \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --no-cache-dir --break-system-packages numpy numba pillow

# KiCad's default global library tables: without them kicad-cli DRC can't find the stock footprint libraries
# and flags every footprint with "lib_footprint_issues"
RUN mkdir -p /root/.config/kicad/9.0  && cp /usr/share/kicad/template/fp-lib-table /usr/share/kicad/template/sym-lib-table /root/.config/kicad/9.0/

# where tools/_env.py and copilot/kicad.py look for KiCad
ENV AUTOPCB_KPY=python3 \
    KICAD_CLI=kicad-cli \
    KICAD_SHARE=/usr/share/kicad \
    PYTHONUNBUFFERED=1 \
    AUTOPCB_DOCKER=1 \
    NUMBA_CACHE_DIR=/tmp/numba-cache

WORKDIR /work
