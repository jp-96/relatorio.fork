## See here for image contents: https://github.com/microsoft/vscode-dev-containers/blob/v0.163.0/containers/python-3/.devcontainer/base.Dockerfile
## Available image variants: https://github.com/microsoft/vscode-dev-containers/blob/main/containers/python-3/README.md
ARG VARIANT="3"
FROM mcr.microsoft.com/vscode/devcontainers/python:0-${VARIANT}

# [Optional] Allow the vscode user to pip install globally w/o sudo
RUN pip3 install --upgrade pip
ENV PIP_TARGET=/usr/local/pip-global
ENV PYTHONPATH=${PIP_TARGET}:${PYTHONPATH}
ENV PATH=${PIP_TARGET}/bin:${PATH}
RUN if ! cat /etc/group | grep -e "^pip-global:" > /dev/null 2>&1; then groupadd -r pip-global; fi \
    && usermod -a -G pip-global vscode \
    && umask 0002 && mkdir -p ${PIP_TARGET} \
    && chown :pip-global ${PIP_TARGET} \
    && ( [ ! -f "/etc/profile.d/00-restore-env.sh" ] || sed -i -e "s/export PATH=/export PATH=\/usr\/local\/pip-global:/" /etc/profile.d/00-restore-env.sh )

# [Option] Install Node.js
# ARG INSTALL_NODE="true"
# ARG NODE_VERSION="lts/*"
# RUN if [ "${INSTALL_NODE}" = "true" ]; then su vscode -c "umask 0002 && . /usr/local/share/nvm/nvm.sh && nvm install ${NODE_VERSION} 2>&1"; fi

# [Optional] If your pip requirements rarely change, uncomment this section to add them to the image.
# COPY requirements.txt /tmp/pip-tmp/
# RUN pip3 --disable-pip-version-check --no-cache-dir install -r /tmp/pip-tmp/requirements.txt \
#    && rm -rf /tmp/pip-tmp

# [Optional] Uncomment this section to install additional OS packages.
# RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
#     && apt-get -y install --no-install-recommends <your-package-list-here>

# [Optional] Uncomment this line to install global node packages.
# RUN su vscode -c "source /usr/local/share/nvm/nvm.sh && npm install -g <your-package-here>" 2>&1

# Create directory and set permission
RUN mkdir /opt/dev \ 
    && chown -R vscode:vscode /opt/dev
RUN mkdir /opt/dev/.vscode \
    && chown vscode:vscode /opt/dev/.vscode
RUN mkdir /home/vscode/.vscode-server \
    && chown vscode:vscode /home/vscode/.vscode-server

# Install pycairo, 
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get -y install --no-install-recommends \
        # - https://pycairo.readthedocs.io/en/latest/getting_started.html
        libcairo2-dev pkg-config python3-dev \
        # texexec
        context

# [Optional] If your pip requirements rarely change, uncomment this section to add them to the image.
COPY ./requirements_dev.txt /tmp/pip-tmp/
RUN pip3 --disable-pip-version-check --no-cache-dir install -r /tmp/pip-tmp/requirements_dev.txt \
    && rm -rf /tmp/pip-tmp
