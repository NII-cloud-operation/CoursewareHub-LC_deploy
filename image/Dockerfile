FROM niicloudoperation/notebook

USER root

RUN mamba install --quiet --yes awscli passlib && mamba clean --all -f -y

USER $NB_USER
