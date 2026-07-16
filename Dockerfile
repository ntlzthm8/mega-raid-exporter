FROM debian:bookworm-slim AS storcli

ARG STORCLI_ARCHIVE=SAS35_StorCLI_7_23-007.2310.0000.0000.zip
ARG STORCLI_SHA256=a6470084f332782e177c016779b5484a446aee3efcc027c0337b3ab0bac32217

RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip dpkg xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/storcli
COPY ${STORCLI_ARCHIVE} /tmp/storcli/storcli.zip
RUN echo "${STORCLI_SHA256}  storcli.zip" | sha256sum -c - \
    && unzip -q storcli.zip \
    && unzip -q storcli_rel/Unified_storcli_all_os.zip \
    && dpkg-deb -x Unified_storcli_all_os/Ubuntu/storcli_007.2310.0000.0000_all.deb /out

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=storcli /out/opt/MegaRAID/storcli/storcli64 /opt/MegaRAID/storcli/storcli64
COPY mega_raid_exporter.py /app/mega_raid_exporter.py

RUN chmod 0755 /opt/MegaRAID/storcli/storcli64 /app/mega_raid_exporter.py

EXPOSE 9634
USER 0
ENTRYPOINT ["python", "/app/mega_raid_exporter.py"]
