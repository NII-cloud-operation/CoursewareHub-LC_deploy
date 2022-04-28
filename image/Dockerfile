FROM niicloudoperation/notebook

USER root

RUN mamba install --quiet --yes awscli passlib && mamba clean --all -f -y

# Workaround to fix Jupyter-LC_wrapper issue with jupyter_client 7.2.2
RUN mamba update --quiet --yes jupyter_client && mamba clean --all -f -y
#RUN pip install --no-cache-dir --upgrade jupyter-client

USER $NB_USER
