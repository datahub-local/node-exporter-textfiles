FROM public.ecr.aws/docker/library/python:3.12-bookworm

RUN apt-get -q update && \
    apt-get -q upgrade -y && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        jq \
        moreutils \
        nvme-cli \
        pciutils \
        smartmontools \
        wget && \
    mkdir -p /scripts && \
    git clone --depth 1 --branch master --single-branch \
        https://github.com/prometheus-community/node-exporter-textfile-collector-scripts.git \
        /scripts && \
    chmod 755 /scripts/* && \
    /usr/sbin/update-smart-drivedb && \
    apt-get remove -y gpg git gpg-agent && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* && \
    update-pciids -q

COPY entrypoint.sh /entrypoint.sh
COPY requirements.txt /requirements.txt
COPY scripts/* /scripts/

RUN pip install --no-cache-dir -r /requirements.txt

RUN chmod 755 /entrypoint.sh /scripts/*.sh /scripts/*.py

ENTRYPOINT ["/entrypoint.sh"]