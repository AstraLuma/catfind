#| build: prod
#| build: dev

#: The version of python to use. Can't be set willy-nilly.
# Note: Python version needs to be kept in sync with pyproject.toml
ARG PYVER=3.9

# Temporary container to handle building python-ldap
FROM python:${PYVER} AS builder
RUN pip install poetry
WORKDIR /project
# precompile everything
COPY . .
# Protection from environment polution
RUN if [ -d dist ]; then rm -r dist; fi
RUN poetry build
# No hashes because gunicorn requires setuptools but this doesn't include it
RUN poetry export --without-hashes -o requirements.txt
WORKDIR /project/dist
RUN pip wheel -r ../requirements.txt
RUN ls

FROM python:${PYVER}-slim
LABEL maintainer="Jamie Bliss <jamie@ivyleav.es>"

# Install the wheels downloaded in the temporary container
COPY --from=builder /project/dist/*.whl /tmp/
RUN pip install --disable-pip-version-check /tmp/*.whl && \
    rm /tmp/*.whl

ENV FLASK_APP=catfind

CMD ["gunicorn", "-b", "0.0.0.0:8000", "catfind:app"]
