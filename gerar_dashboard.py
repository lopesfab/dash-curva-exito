import xmlrpc.client
import json
import math
from collections import defaultdict
from datetime import datetime
import copy

# ── CONFIG ──────────────────────────────────────────────────────────────────
import os
ODOO_URL      = os.environ.get("ODOO_URL",      "https://mmp.intelligenti.com.br/")
ODOO_DB       = os.environ.get("ODOO_DB",       "mmp.intelligenti.com.br")
ODOO_USER     = os.environ.get("ODOO_USERNAME", "Intel_bot_andamento")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "Intel_bot_andamento")

# Tipos de sentença que representam ÊXITO para o réu
EXITO_SENTENCA   = {"improcedente", "improcedente ", "extinção", "extincao", "extinção sem resolução", "extinção com resolução"}
# No acórdão: se existe tipo_sentenca_modificada_id, ele prevalece com mesma lógica invertida
DERROTA_SENTENCA = {"procedente", "parcialmente procedente", "procedente em parte", "parcialmente procedente "}

MIN_N = 30  # volume mínimo para considerar recorte


# ── CONEXÃO ─────────────────────────────────────────────────────────────────
def conectar():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise Exception("Falha na autenticação XMLRPC")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


# ── BUSCA DE DADOS ───────────────────────────────────────────────────────────
def buscar_processos(uid, models):
    """Busca todos os processos com sentença definida"""
    campos = [
        "id", "name",
        "carteira_metas_id",
        "grupo_id",
        "comarca_id",
        "estado_id",
        "data_sentenca",
        "tipo_sentenca_id",
        "data_acordao",
        "tipo_sentenca_modificada_id",
    ]

    # Data limite: últimos 18 meses a partir de hoje
    from datetime import timedelta
    data_limite = (datetime.today() - timedelta(days=18 * 30)).strftime("%Y-%m-%d")

    # Grupos permitidos (nome exato como cadastrado no Odoo)
    GRUPOS_PERMITIDOS = [
        "Cielo",
        "Mercantil",
        "Bradesco",
        "Gol",
        "Santander",
        "Verisure",
        "Ccb Brasil - China Construction Bank",
        "Movida",
        "Polishop",
    ]

    # Busca os IDs dos grupos pelo nome no modelo res.partner (ou modelo do grupo_id)
    grupo_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search",
        [[["name", "in", GRUPOS_PERMITIDOS]]],
        {}
    )
    if not grupo_ids:
        raise Exception("Nenhum grupo encontrado. Verifique os nomes exatos no Odoo.")
    print(f"Grupos encontrados: {len(grupo_ids)} IDs → {grupo_ids}")

    # Filtro: sentença nos últimos 18 meses + carteira + tipo sentença + grupo permitido
    domain = [
        ["data_sentenca", "!=", False],
        ["data_sentenca", ">=", data_limite],
        ["carteira_metas_id", "!=", False],
        ["tipo_sentenca_id", "!=", False],
        ["grupo_id", "in", grupo_ids],
    ]
    print(f"Filtrando sentenças a partir de: {data_limite}")
    print("Buscando processos no Odoo...")
    # Busca em lotes
    registros = []
    offset = 0
    batch = 1000
    while True:
        lote = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "dossie.dossie", "search_read",
            [domain],
            {"fields": campos, "limit": batch, "offset": offset}
        )
        if not lote:
            break
        registros.extend(lote)
        offset += batch
        print(f"  {len(registros)} registros carregados...")
        if len(lote) < batch:
            break

    print(f"Total: {len(registros)} processos com sentença")
    return registros


# ── LÓGICA DE ÊXITO ──────────────────────────────────────────────────────────
def calcular_exito(rec):
    """
    Retorna True se o processo foi êxito para o réu.
    Se existe acórdão (tipo_sentenca_modificada_id), prevalece.
    """
    # Acórdão prevalece quando existe
    if rec.get("tipo_sentenca_modificada_id"):
        nome = rec["tipo_sentenca_modificada_id"][1].strip().lower()
        if nome in EXITO_SENTENCA:
            return True
        if nome in DERROTA_SENTENCA:
            return False
        # Se não reconhecido, cai para sentença original

    # Sentença original
    if rec.get("tipo_sentenca_id"):
        nome = rec["tipo_sentenca_id"][1].strip().lower()
        if nome in EXITO_SENTENCA:
            return True
        if nome in DERROTA_SENTENCA:
            return False

    return None  # indefinido


def quarter(data_str):
    """Converte 'YYYY-MM-DD' em 'YYYYQn'"""
    try:
        d = datetime.strptime(data_str[:10], "%Y-%m-%d")
        q = (d.month - 1) // 3 + 1
        return f"{d.year}Q{q}"
    except:
        return None


# ── ESTATÍSTICA ──────────────────────────────────────────────────────────────
def binomial_p(k, n, p0):
    """
    Teste binomial unicaudal esquerdo: P(X <= k | n, p0)
    Retorna p-valor para H1: p < p0 (recorte pior que média)
    """
    if n == 0:
        return 1.0
    # Usa aproximação normal para n > 30
    if n > 30:
        p_hat = k / n
        se = math.sqrt(p0 * (1 - p0) / n)
        if se == 0:
            return 1.0
        z = (p_hat - p0) / se
        # CDF normal aproximada
        return normal_cdf(z)
    # Exato para n pequeno
    from math import comb
    total = sum(comb(n, i) * (p0 ** i) * ((1 - p0) ** (n - i)) for i in range(k + 1))
    return min(total, 1.0)


def normal_cdf(z):
    """Approximação da CDF normal"""
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def binomial_p_favoravel(k, n, p0):
    """p-valor para H1: p > p0 (recorte melhor que média)"""
    return 1 - binomial_p(k - 1, n, p0)


def calcular_trend(global_periods):
    """Regressão linear simples sobre os períodos"""
    ps = [p for p in global_periods if p["n"] >= 5]
    if len(ps) < 3:
        return {"trend": "insuficiente", "slope_pp_trim": 0.0, "trend_p": 1.0}
    xs = list(range(len(ps)))
    ys = [p["exito"] for p in ps]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    ss_xy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    ss_xx = sum((xs[i] - mx) ** 2 for i in range(n))
    if ss_xx == 0:
        return {"trend": "estavel", "slope_pp_trim": 0.0, "trend_p": 1.0}
    slope = ss_xy / ss_xx

    # p-valor aproximado da inclinação
    y_pred = [my + slope * (xs[i] - mx) for i in range(n)]
    ss_res = sum((ys[i] - y_pred[i]) ** 2 for i in range(n))
    se_slope = math.sqrt(ss_res / max(n - 2, 1) / ss_xx) if ss_xx > 0 else 0
    t = slope / se_slope if se_slope > 0 else 0
    # Aproximação p-valor two-sided via normal
    p_val = 2 * (1 - normal_cdf(abs(t)))

    slope_r = round(slope, 2)
    if p_val < 0.10:
        trend = "caindo" if slope < 0 else "subindo"
    else:
        trend = "estavel"

    return {"trend": trend, "slope_pp_trim": slope_r, "trend_p": round(p_val, 3)}


# ── CONSTRUÇÃO DO OBJETO D ───────────────────────────────────────────────────
def construir_D(registros):
    # Organiza por carteira
    por_carteira = defaultdict(list)
    grupos = {}

    for rec in registros:
        if not rec.get("carteira_metas_id"):
            continue
        cart_id, cart_nome = rec["carteira_metas_id"]
        por_carteira[cart_nome].append(rec)
        if rec.get("grupo_id"):
            grupos[cart_nome] = rec["grupo_id"][1]

    # ── CARTEIRAS ────────────────────────────────────────────────────────────
    carteiras_list = []
    details = {}
    evolution = {}

    for cart_nome, recs in por_carteira.items():
        validos = [(r, calcular_exito(r)) for r in recs]
        validos = [(r, e) for r, e in validos if e is not None]
        if not validos:
            continue

        n_total = len(validos)
        n_exito = sum(1 for _, e in validos if e)
        pct_exito = round(n_exito / n_total * 100, 1) if n_total else 0

        # Granularidade: verifica quantas comarcas/UFs únicas com n >= MIN_N
        comarcas_count = defaultdict(lambda: [0, 0])
        ufs_count = defaultdict(lambda: [0, 0])
        for rec, ex in validos:
            if rec.get("comarca_id"):
                c = rec["comarca_id"][1]
                comarcas_count[c][0] += 1
                comarcas_count[c][1] += (1 if ex else 0)
            if rec.get("estado_id"):
                u = rec["estado_id"][1] if isinstance(rec["estado_id"], list) else rec["estado_id"]
                # estado_id pode vir como [id, name] ou string
                if isinstance(u, list):
                    u = u[1]
                ufs_count[u][0] += 1
                ufs_count[u][1] += (1 if ex else 0)

        com_validas = [c for c, v in comarcas_count.items() if v[0] >= MIN_N]
        uf_validas  = [u for u, v in ufs_count.items() if v[0] >= MIN_N]
        n_coms = len(com_validas)
        n_ufs  = len(uf_validas)

        if n_coms >= 5:
            granularity = "ambos" if n_ufs >= 5 else "comarca"
        elif n_ufs >= 5:
            granularity = "uf"
        else:
            granularity = "agregado"

        carteiras_list.append({
            "nome": cart_nome,
            "grupo": grupos.get(cart_nome, "—"),
            "n": n_total,
            "exito": pct_exito,
            "granularity": granularity,
            "n_ufs_30": n_ufs,
            "n_coms_30": n_coms,
        })

        # ── DETAILS (ofensores) ──────────────────────────────────────────────
        media_p = n_exito / n_total if n_total else 0

        def calcular_recortes(agrupamento):
            resultado = []
            for nome_rec, (n_rec, ex_rec) in agrupamento.items():
                if n_rec < MIN_N:
                    continue
                exito_rec = ex_rec / n_rec
                gap = round((exito_rec - media_p) * 100, 1)
                pct = round(n_rec / n_total * 100, 1)
                exito_pct = round(exito_rec * 100, 1)

                if gap < 0:
                    p = round(binomial_p(ex_rec, n_rec, media_p), 4)
                    tipo = "ofensor" if p < 0.05 else "neutro"
                else:
                    p = round(binomial_p_favoravel(ex_rec, n_rec, media_p), 4)
                    tipo = "favoravel" if p > 0.95 else "neutro"

                resultado.append({
                    "recorte": nome_rec,
                    "n": n_rec,
                    "pct": pct,
                    "exito": exito_pct,
                    "media": pct_exito,
                    "gap": gap,
                    "p": p,
                    "tipo": tipo,
                })
            resultado.sort(key=lambda x: x["gap"])
            return resultado

        com_dict = {k: tuple(v) for k, v in comarcas_count.items()}
        uf_dict  = {k: tuple(v) for k, v in ufs_count.items()}

        det_com = calcular_recortes(com_dict) if granularity in ("comarca", "ambos") else []
        det_uf  = calcular_recortes(uf_dict)  if granularity in ("uf", "ambos") else []
        details[cart_nome] = {"uf": det_uf, "com": det_com}

        # ── EVOLUTION ────────────────────────────────────────────────────────
        por_quarter = defaultdict(lambda: [0, 0])
        for rec, ex in validos:
            q = quarter(rec.get("data_sentenca", "") or "")
            if q:
                por_quarter[q][0] += 1
                por_quarter[q][1] += (1 if ex else 0)

        global_periods = []
        for q in sorted(por_quarter.keys()):
            n_q, ex_q = por_quarter[q]
            if n_q >= 5:
                global_periods.append({
                    "q": q,
                    "n": n_q,
                    "exito": round(ex_q / n_q * 100, 1)
                })

        trend_info = calcular_trend(global_periods)

        evo_entry = {
            "global": global_periods,
            "trend": trend_info["trend"],
            "slope_pp_trim": trend_info["slope_pp_trim"],
            "trend_p": trend_info["trend_p"],
            "n": n_total,
            "exito": pct_exito,
            "granularity": granularity,
        }

        # Evolução por comarca
        if granularity in ("comarca", "ambos"):
            com_evol = {}
            por_com_q = defaultdict(lambda: defaultdict(lambda: [0, 0]))
            for rec, ex in validos:
                if rec.get("comarca_id"):
                    c = rec["comarca_id"][1]
                    q = quarter(rec.get("data_sentenca", "") or "")
                    if q:
                        por_com_q[c][q][0] += 1
                        por_com_q[c][q][1] += (1 if ex else 0)

            for c, q_data in por_com_q.items():
                total_c = comarcas_count[c][0]
                if total_c < MIN_N:
                    continue
                periods_c = []
                for q in sorted(q_data.keys()):
                    n_q, ex_q = q_data[q]
                    if n_q >= 5:
                        periods_c.append({"q": q, "n": n_q, "exito": round(ex_q / n_q * 100, 1)})
                if len(periods_c) >= 2:
                    # Agrupa em semestre se volume médio < 12/trimestre
                    avg_n = sum(p["n"] for p in periods_c) / len(periods_c)
                    period_type = "trimestral" if avg_n >= 12 else "semestral"
                    if period_type == "semestral":
                        # Reagrupa por semestre
                        sem_data = defaultdict(lambda: [0, 0])
                        for rec2, ex2 in validos:
                            if rec2.get("comarca_id") and rec2["comarca_id"][1] == c:
                                q2 = quarter(rec2.get("data_sentenca", "") or "")
                                if q2:
                                    yr, qt = q2[:4], int(q2[5])
                                    sem = f"{yr}-S{1 if qt <= 2 else 2}"
                                    sem_data[sem][0] += 1
                                    sem_data[sem][1] += (1 if ex2 else 0)
                        periods_c = [{"q": s, "n": v[0], "exito": round(v[1]/v[0]*100, 1)}
                                     for s, v in sorted(sem_data.items()) if v[0] >= 5]

                    if len(periods_c) >= 2:
                        delta = round(periods_c[-1]["exito"] - periods_c[0]["exito"], 1)
                        com_evol[c] = {
                            "periods": periods_c,
                            "period_type": period_type,
                            "total_n": total_c,
                            "delta": delta,
                        }
            # Ordena por total_n desc
            com_evol = dict(sorted(com_evol.items(), key=lambda x: -x[1]["total_n"]))
            evo_entry["com_evol"] = com_evol

        # Evolução por UF
        if granularity in ("uf", "ambos"):
            uf_evol = {}
            por_uf_q = defaultdict(lambda: defaultdict(lambda: [0, 0]))
            for rec, ex in validos:
                if rec.get("estado_id"):
                    u = rec["estado_id"]
                    if isinstance(u, list):
                        u = u[1]
                    q = quarter(rec.get("data_sentenca", "") or "")
                    if q:
                        por_uf_q[u][q][0] += 1
                        por_uf_q[u][q][1] += (1 if ex else 0)

            for u, q_data in por_uf_q.items():
                total_u = ufs_count[u][0]
                if total_u < MIN_N:
                    continue
                periods_u = []
                for q in sorted(q_data.keys()):
                    n_q, ex_q = q_data[q]
                    if n_q >= 5:
                        periods_u.append({"q": q, "n": n_q, "exito": round(ex_q / n_q * 100, 1)})
                if len(periods_u) >= 2:
                    avg_n = sum(p["n"] for p in periods_u) / len(periods_u)
                    period_type = "trimestral" if avg_n >= 12 else "semestral"
                    if period_type == "semestral":
                        sem_data = defaultdict(lambda: [0, 0])
                        for rec2, ex2 in validos:
                            u2 = rec2.get("estado_id")
                            if u2:
                                if isinstance(u2, list):
                                    u2 = u2[1]
                                if u2 == u:
                                    q2 = quarter(rec2.get("data_sentenca", "") or "")
                                    if q2:
                                        yr, qt = q2[:4], int(q2[5])
                                        sem = f"{yr}-S{1 if qt <= 2 else 2}"
                                        sem_data[sem][0] += 1
                                        sem_data[sem][1] += (1 if ex2 else 0)
                        periods_u = [{"q": s, "n": v[0], "exito": round(v[1]/v[0]*100, 1)}
                                     for s, v in sorted(sem_data.items()) if v[0] >= 5]
                    if len(periods_u) >= 2:
                        delta = round(periods_u[-1]["exito"] - periods_u[0]["exito"], 1)
                        uf_evol[u] = {
                            "periods": periods_u,
                            "period_type": period_type,
                            "total_n": total_u,
                            "delta": delta,
                        }
            uf_evol = dict(sorted(uf_evol.items(), key=lambda x: -x[1]["total_n"]))
            evo_entry["uf_evol"] = uf_evol

        evolution[cart_nome] = evo_entry

    # Ordena carteiras por n desc
    carteiras_list.sort(key=lambda x: -x["n"])

    return {
        "carteiras": carteiras_list,
        "details": details,
        "evolution": evolution,
        "objetos": {},
        "priorizacao": {},
    }


# ── INJETAR NO HTML ──────────────────────────────────────────────────────────
def gerar_html(D_obj, template_path, output_path):
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    d_json = json.dumps(D_obj, ensure_ascii=False)

    # Substitui o bloco "const D={...};" no HTML
    import re
    novo_html = re.sub(
        r'const D=\{.*?\};',
        f'const D={d_json};',
        html,
        count=1,
        flags=re.DOTALL
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(novo_html)

    print(f"HTML gerado: {output_path}")


# ── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    template = sys.argv[1] if len(sys.argv) > 1 else "analise_curva_exito__3_.html"
    output   = sys.argv[2] if len(sys.argv) > 2 else "dashboard_live.html"

    uid, models = conectar()
    registros = buscar_processos(uid, models)
    D_obj = construir_D(registros)

    print(f"\nResumo gerado:")
    print(f"  Carteiras: {len(D_obj['carteiras'])}")
    for c in D_obj["carteiras"]:
        print(f"    {c['nome']}: n={c['n']}, êxito={c['exito']}%, gran={c['granularity']}")

    gerar_html(D_obj, template, output)
    print("\nPronto! Abra o arquivo:", output)
