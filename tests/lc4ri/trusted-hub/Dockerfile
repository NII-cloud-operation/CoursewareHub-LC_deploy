ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY test-ca.crt /usr/local/share/ca-certificates/coursewarehub-test.crt
RUN update-ca-certificates
