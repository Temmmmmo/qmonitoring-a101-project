FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system rebar \
    && useradd --system --gid rebar --home-dir /app --create-home rebar

WORKDIR /app

COPY pyproject.toml setup.py MANIFEST.in README.md requirements-runtime.txt ./
COPY src ./src
COPY integrations/pyrevit/QMonitoring.extension/lib ./integrations/pyrevit/QMonitoring.extension/lib
COPY integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/WorkingHostProbe.pushbutton ./integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/WorkingHostProbe.pushbutton
COPY integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/WorkingRebarProbe.pushbutton ./integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/WorkingRebarProbe.pushbutton
COPY integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/PlanPreview.pushbutton ./integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/PlanPreview.pushbutton
COPY integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Workflow.panel/SourceWorkflow.pushbutton ./integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Workflow.panel/SourceWorkflow.pushbutton

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-runtime.txt \
    && python -m pip install --no-deps .

USER rebar

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "rebar.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
