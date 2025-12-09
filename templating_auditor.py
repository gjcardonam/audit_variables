#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auditor de VARIABLES (templating list) para Grafana.
Soporta múltiples entornos y autenticación híbrida (Token o Basic Auth).
"""

import os, sys, json, time, uuid, argparse
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

# Cargar variables de entorno desde .env si existe
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(), override=True)
except Exception:
    pass

# BD opcional
try:
    import psycopg2, psycopg2.extras
except Exception:
    psycopg2 = None

# ---------- util ----------

def progress_bar(done: int, total: int, prefix: str = "", width: int = 46) -> None:
    if total <= 0: total = 1
    ratio = max(0.0, min(1.0, done / total))
    filled = int(width * ratio)
    bar = "█" * filled + "░" * (width - filled)
    sys.stdout.write(f"\r{prefix} [{bar}] {done}/{total} ({ratio*100:5.1f}%)")
    sys.stdout.flush()

def load_json(path: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No se encontró el archivo: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def fmt_sec(s: float) -> int:
    return int(s if s >= 0 else 0)

def norm(s: Optional[str]) -> str:
    return (s or "").strip().casefold()

def resolve_secret(val: Any) -> Any:
    """Si val empieza con ENV_, busca en variables de entorno."""
    if isinstance(val, str) and val.startswith("ENV_"):
        env_key = val[4:]
        return os.getenv(env_key, os.getenv(val, val))
    return val

def _bool_env_default(val, default_true=True):
    if isinstance(val, bool): return val
    if val is None: return default_true
    return str(val).lower() not in {"0", "false", "no"}

# ---------- config ----------

def _resolve_grafana_fields(env_block: dict) -> Dict[str, Any]:
    g = env_block.get("grafana", {}) if env_block else {}
    grafana_url = (g.get("url") or os.getenv("GRAFANA_URL", "")).rstrip("/")
    
    # Resolvemos credenciales globales (fallback o legacy)
    grafana_user = resolve_secret(g.get("username", ""))
    grafana_pass = resolve_secret(g.get("password", ""))

    if not grafana_user: grafana_user = os.getenv("GRAFANA_USERNAME", "")
    if not grafana_pass: grafana_pass = os.getenv("GRAFANA_PASSWORD", "")

    verify_ssl = _bool_env_default(os.getenv("VERIFY_SSL", g.get("verify_ssl", True)), True)

    companies_inline = g.get("companies_inline")
    companies_file = os.getenv("COMPANIES_FILE", g.get("companies_file"))

    return {
        "url": grafana_url,
        "username": grafana_user,
        "password": grafana_pass,
        "verify_ssl": verify_ssl,
        "companies_inline": companies_inline,
        "companies_file": companies_file,
    }

def _resolve_rules(cfg_rules: dict) -> Dict[str, Any]:
    r = cfg_rules or {}
    ignore_folders = [s.lower() for s in r.get("ignore_folders", ["Test"])]
    return {"ignore_folders": ignore_folders}

def _resolve_db(cfg_db: dict) -> Dict[str, Any]:
    d = cfg_db or {}
    return {
        "enabled": bool(d.get("enabled", False)),
        "host": os.getenv("DB_HOST", d.get("host", "")),
        "port": int(os.getenv("DB_PORT", str(d.get("port", 5432)))),
        "name": os.getenv("DB_NAME", d.get("name", "postgres")),
        "schema": os.getenv("DB_SCHEMA", d.get("schema", "public")),
        "user": os.getenv("DB_USER", d.get("user", "")),
        "password": resolve_secret(d.get("password", "")),
    }

def build_config(args) -> Dict[str, Any]:
    cfg = load_json(args.config)
    
    environments = cfg.get("environments", [])
    # Retrocompatibilidad para configs viejos
    if not environments and "grafana" in cfg:
        env_name = cfg.get("env_name", os.getenv("ENV_NAME", "dev"))
        environments = [{"name": env_name, "grafana": cfg.get("grafana", {})}]

    envs_resolved = []
    for env in environments:
        name = env.get("name") or os.getenv("ENV_NAME", "dev")
        grafana = _resolve_grafana_fields(env)
        envs_resolved.append({"name": name, "grafana": grafana})

    rules = _resolve_rules(cfg.get("rules", {}))
    db = _resolve_db(cfg.get("db", {}))
    progress = {"bar_width": int(cfg.get("progress", {}).get("bar_width", 46))}

    out_cfg = cfg.get("output", {})
    env_write_json = os.getenv("WRITE_JSON")
    env_write_csv = os.getenv("WRITE_CSV")

    write_json = _bool_env_default(env_write_json, True) if env_write_json is not None else _bool_env_default(out_cfg.get("write_json", True), True)
    write_csv = _bool_env_default(env_write_csv, True) if env_write_csv is not None else _bool_env_default(out_cfg.get("write_csv", True), True)

    return {"environments": envs_resolved, "rules": rules, "db": db, "progress": progress, "output": {"write_json": write_json, "write_csv": write_csv}}

# ---------- grafana http ----------

def make_base_session(verify_ssl: bool):
    """Crea sesión base sin auth."""
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    s.verify = verify_ssl
    return s

def switch_organization(session: requests.Session, base_url: str, org_id: int) -> None:
    r = session.post(f"{base_url}/user/using/{org_id}")
    if r.status_code != 200:
        raise RuntimeError(f"Switch org {org_id} failed: {r.status_code} {r.text}")

def search_folders(session: requests.Session, base_url: str, q: str) -> List[Dict[str, Any]]:
    r = session.get(f"{base_url}/search", params={"type": "dash-folder", "query": q, "limit": 5000})
    r.raise_for_status()
    return r.json() or []

def search_dashboards_any(session: requests.Session, base_url: str) -> List[Dict[str, Any]]:
    r = session.get(f"{base_url}/search", params={"type": "dash-db", "limit": 5000})
    r.raise_for_status()
    return r.json() or []

def get_dashboard(session: requests.Session, base_url: str, uid: str) -> Dict[str, Any]:
    r = session.get(f"{base_url}/dashboards/uid/{uid}")
    r.raise_for_status()
    return r.json()

# ---------- helpers ----------

def find_company_folder(session, base_url: str, company_name: str) -> Optional[Dict[str, Any]]:
    for it in search_folders(session, base_url, company_name):
        if norm(it.get("title")) == norm(company_name):
            return it
    return None

def as_list(value):
    if isinstance(value, list): return value
    if value is None: return []
    return [value]

def is_hidden(var: Dict[str, Any]) -> bool:
    return (var.get("hide", 0) or 0) != 0

def as_int(x, default=0):
    try: return int(x)
    except Exception: return default

# ---------- validaciones templating ----------

def validate_variables(vars_list: List[Dict[str, Any]]) -> List[Dict[str, Optional[str]]]:
    issues: List[Dict[str, Optional[str]]] = []
    def add(issue: str, var_name: Optional[str] = None):
        issues.append({"issue": issue, "var_name": var_name})

    if vars_list is None:
        add("templating_missing", None)
        return issues

    idx_schema = idx_group = None
    for i, v in enumerate(vars_list):
        n_norm = norm(v.get("name"))
        if n_norm == "schema" and idx_schema is None: idx_schema = i
        if n_norm == "groupid" and idx_group is None: idx_group = i

    if len(vars_list) < 2:
        add("vars_too_few_for_schema_groupId", None)
        return issues
    
    v0 = (vars_list[0].get("name") or "").strip()
    v1 = (vars_list[1].get("name") or "").strip()
    if v0 != "schema": add("first_var_must_be_schema", "schema")
    if v1 != "groupId": add("second_var_must_be_groupId", "groupId")

    if idx_schema is None: add("schema_missing", "schema")
    elif not is_hidden(vars_list[idx_schema]): add("schema_must_be_hidden", "schema")

    if idx_group is None: add("groupId_missing", "groupId")
    elif not is_hidden(vars_list[idx_group]): add("groupId_must_be_hidden", "groupId")

    cut_idx = len(vars_list)
    for i, v in enumerate(vars_list):
        if norm(v.get("name")) in ("some_nodeid", "all_nodeid", "nodeid"):
            cut_idx = i
            break

    for i, v in enumerate(vars_list):
        n_raw = (v.get("name") or "").strip()
        n_norm = norm(n_raw)
        if n_norm in ("schema", "groupid"): continue

        if n_norm == "scale":
            if is_hidden(v): add("scale_must_be_visible", n_raw or "scale")
            continue

        if n_norm == "some_nodeid":
            if is_hidden(v): add("some_nodeId_must_be_visible", n_raw)
            if v.get("multi") is not True: add("some_nodeId_multi_true", n_raw)
            if v.get("includeAll") is not True: add("some_nodeId_includeAll_true", n_raw)
            cur = v.get("current", {}) or {}
            txt, val = as_list(cur.get("text")), as_list(cur.get("value"))
            if not (("All" in txt) and ("$__all" in val)): add("some_nodeId_current_must_be_All", n_raw)
            continue

        if n_norm == "all_nodeid":
            if not is_hidden(v): add("nonfilter_all_nodeId_must_be_hidden", n_raw)
            if v.get("includeAll") is not True: add("all_nodeId_includeAll_true", n_raw)
            cur = v.get("current", {}) or {}
            txt, val = as_list(cur.get("text")), as_list(cur.get("value"))
            if not (("All" in txt) and ("$__all" in val)): add("all_nodeId_current_must_be_All", n_raw)
            continue

        if n_norm == "nodeid":
            if not is_hidden(v): add("nonfilter_nodeId_must_be_hidden", n_raw)
            if v.get("includeAll") is True: add("nodeId_includeAll_must_be_false", n_raw)
            continue

        is_filter = (idx_group is not None) and (i > idx_group) and (i < cut_idx)
        if is_filter:
            if is_hidden(v): add(f"filter_{n_raw}_must_be_visible", n_raw)
            if v.get("multi") is not True: add(f"filter_{n_raw}_multi_true", n_raw)
            if v.get("includeAll") is not True: add(f"filter_{n_raw}_includeAll_true", n_raw)
            if as_int(v.get("sort"), 0) != 1: add(f"filter_{n_raw}_sort_must_be_1", n_raw)
            cur = v.get("current", {}) or {}
            txt, val = as_list(cur.get("text")), as_list(cur.get("value"))
            if not (("All" in txt) and ("$__all" in val)): add(f"filter_{n_raw}_current_must_be_All", n_raw)
        else:
            if not is_hidden(v): add(f"nonfilter_{n_raw}_must_be_hidden", n_raw)

    return issues

# ---------- núcleo ----------

def run_for_environment(env_cfg: Dict[str, Any], rules: Dict[str, Any], db_cfg: Dict[str, Any], bar_width: int, output_cfg: Dict[str, Any]) -> Dict[str, Any]:
    g = env_cfg["grafana"]
    env_name = env_cfg["name"]

    # Determinar lista de compañías (inline o file)
    if g.get("companies_inline"):
        companies = g["companies_inline"]
    elif g.get("companies_file"):
        companies = load_json(g["companies_file"])
    else:
        print(f"\n⚠ Skipping env '{env_name}': No companies defined.")
        return {"summary": {}, "violations": []}

    session = make_base_session(g["verify_ssl"])
    base_url = g["url"]

    # Credenciales globales para fallback
    global_user = g["username"]
    global_pass = g["password"]

    # Configurar DB
    conn = None
    run_id = str(uuid.uuid4())
    if db_cfg["enabled"]:
        try:
            conn = psycopg2.connect(host=db_cfg["host"], port=db_cfg["port"], dbname=db_cfg["name"], user=db_cfg["user"], password=db_cfg["password"])
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {db_cfg['schema']}.templating_audit_runs (run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT now(), ended_at TIMESTAMPTZ, environment TEXT, org_count INTEGER, dashboard_count INTEGER, violations_count INTEGER, elapsed_seconds INTEGER);")
                cur.execute(f"CREATE TABLE IF NOT EXISTS {db_cfg['schema']}.templating_audit_violations (id BIGSERIAL PRIMARY KEY, run_id UUID REFERENCES {db_cfg['schema']}.templating_audit_runs(run_id) ON DELETE CASCADE, org TEXT, dashboard TEXT, dashboard_uid TEXT, folder_title TEXT, folder_id BIGINT, folder_url TEXT, var_name TEXT, issue TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());")
                cur.execute(f"INSERT INTO {db_cfg['schema']}.templating_audit_runs(run_id, environment, org_count, dashboard_count) VALUES (%s,%s,%s,%s)", (run_id, env_name, len(companies), 0))
            conn.commit()
        except Exception as e:
            print(f"Error DB init: {e}")
            return {}

    total_tasks = max(1, len(companies))
    tasks_done = 0
    dash_count = 0
    violations: List[Dict[str, Any]] = []
    start = time.time()

    print(f"\nAuditor TEMPLATING (env={env_name}): {len(companies)} orgs")
    progress_bar(0, total_tasks, f"Progreso {env_name}", bar_width)

    ignore_folders = (rules.get("ignore_folders") or [])

    for company in companies:
        raw_token = company.get("token")
        token = resolve_secret(raw_token)

        # --- AUTH LOGIC (IGUAL QUE TABLE LINKS) ---
        try:
            session.headers.pop("Authorization", None)
            session.auth = None

            if token:
                # CASO 1: TOKEN (Service Account)
                session.headers.update({"Authorization": f"Bearer {token}"})
            elif global_user and global_pass:
                # CASO 2: USER/PASS (Legacy/QA)
                session.auth = HTTPBasicAuth(global_user, global_pass)
                if company.get("id"):
                    switch_organization(session, base_url, company["id"])
            else:
                raise ValueError(f"No credentials for {company.get('name')}")
            
            # --- PROCESAMIENTO ---
            folder = find_company_folder(session, base_url, company["name"])
            if not folder:
                tasks_done += 1
                progress_bar(tasks_done, total_tasks, f"Progreso {env_name}", bar_width)
                continue

            all_dashes = search_dashboards_any(session, base_url)
            in_folder = [it for it in all_dashes if norm(it.get("folderTitle")) == norm(folder.get("title")) and norm(it.get("title")) != "home"]
            
            dash_count += len(in_folder)
            if conn:
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE {db_cfg['schema']}.templating_audit_runs SET dashboard_count=%s WHERE run_id=%s", (dash_count, run_id))
                conn.commit()

            for meta in in_folder:
                uid = meta.get("uid")
                if not uid: continue
                try:
                    payload = get_dashboard(session, base_url, uid)
                except Exception: continue

                dash = payload.get("dashboard") or {}
                dmeta = payload.get("meta") or {}
                folder_title = (dmeta.get("folderTitle") or "").strip().lower()
                
                if folder_title in ignore_folders: continue

                templ = (dash.get("templating") or {}).get("list")
                issues = validate_variables(templ or [])
                for iss in issues:
                    violations.append({
                        "org": company.get("name"), "dashboard": dash.get("title"), "dashboard_uid": dash.get("uid") or dmeta.get("uid"),
                        "folder_title": dmeta.get("folderTitle"), "folder_id": dmeta.get("folderId"), "folder_url": dmeta.get("folderUrl"),
                        "var_name": iss.get("var_name"), "issue": iss.get("issue"),
                    })

        except Exception as e:
            # print(f"Error {company.get('name')}: {e}")
            pass

        tasks_done += 1
        progress_bar(tasks_done, total_tasks, f"Progreso {env_name}", bar_width)

    elapsed = fmt_sec(time.time() - start)
    result = {
        "summary": { "environment": env_name, "organizations": len(companies), "dashboards": dash_count, "violations": len(violations), "elapsed_seconds": elapsed },
        "violations": violations,
    }

    if output_cfg.get("write_json", True): save_json(f"templating_audit_{env_name}.json", result)
    if output_cfg.get("write_csv", True):
        try:
            import csv
            with open(f"templating_audit_{env_name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["org", "dashboard", "dashboard_uid", "folder_title", "folder_id", "folder_url", "var_name", "issue"])
                for v in violations: w.writerow([v.get("org"), v.get("dashboard"), v.get("dashboard_uid"), v.get("folder_title"), v.get("folder_id"), v.get("folder_url"), v.get("var_name"), v.get("issue")])
        except Exception as e: print(f"\n⚠ Error CSV: {e}")

    if conn:
        with conn.cursor() as cur:
            if violations:
                psycopg2.extras.execute_values(cur, f"INSERT INTO {db_cfg['schema']}.templating_audit_violations (run_id, org, dashboard, dashboard_uid, folder_title, folder_id, folder_url, var_name, issue) VALUES %s", 
                    [(run_id, v.get("org"), v.get("dashboard"), v.get("dashboard_uid"), v.get("folder_title"), v.get("folder_id"), v.get("folder_url"), v.get("var_name"), v.get("issue")) for v in violations], page_size=500)
            cur.execute(f"UPDATE {db_cfg['schema']}.templating_audit_runs SET ended_at=now(), violations_count=%s, elapsed_seconds=%s WHERE run_id=%s", (len(violations), elapsed, run_id))
        conn.commit()
        conn.close()

    print(f"\nOK {env_name} TEMPLATING | dashboards={dash_count} viols={len(violations)}")
    return result

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Audita VARIABLES (templating) de Grafana por ambientes con Auth Híbrida.")
    ap.add_argument("-c", "--config", required=True, help="Ruta al JSON de configuración")
    args = ap.parse_args()

    cfg = build_config(args)
    all_results = {"summaries": [], "environments": {}}
    total_tasks, done = max(1, len(cfg["environments"])), 0

    for env in cfg["environments"]:
        res = run_for_environment(env, cfg["rules"], cfg["db"], cfg["progress"]["bar_width"], cfg["output"])
        all_results["summaries"].append(res["summary"])
        all_results["environments"][env["name"]] = res
        done += 1
        progress_bar(done, total_tasks, "Ambientes", cfg["progress"]["bar_width"])

    if cfg["output"].get("write_json", True):
        save_json("templating_audit_all.json", all_results)
        print("\nResumen combinado escrito en templating_audit_all.json")

if __name__ == "__main__":
    main()