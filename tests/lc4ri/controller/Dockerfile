FROM niicloudoperation/notebook:feature-lab
USER root
RUN python3 -m pip install --no-cache-dir awscli==1.46.2 passlib==1.7.4
RUN mkdir -p /home/jovyan/work /home/jovyan/.ssh \
 && chown -R 1000:100 /home/jovyan/work /home/jovyan/.ssh
USER $NB_USER
