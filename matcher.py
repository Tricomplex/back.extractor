"""
matcher_nfe_v2.py
=================
Extrai produtos de uma NF-e XML e encontra O MELHOR produto correspondente
no banco tricomplex usando cascata de confiança:

  Nível 1 — EAN/GTIN exato          → confiança: EXATA    (100)
  Nível 2 — NCM exato (8 dígitos)   → confiança: ALTA      (90)
  Nível 3 — NCM parcial (6 dígitos) → confiança: MEDIA     (70)
  Nível 4 — Fuzzy na descrição      → confiança: BAIXA     (0–65)

Retorna exatamente 1 resultado por item da NF-e.

Uso:
    python matcher_nfe_v2.py <arquivo.xml> [--out resultado.json|.csv]

Dependências:
    pip install mysql-connector-python python-dotenv rapidfuzz
"""

import os
import sys
import json
import csv
import argparse
import logging
import unicodedata
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from rapidfuzz import fuzz

from Extractor.extractor import extrair_nfe

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 3306)),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "tricomplex"),
    "charset":  "utf8mb4",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Níveis de confiança
CONFIANCA = {
    "EXATA":  100,   # EAN bate
    "ALTA":    90,   # NCM 8 dígitos bate
    "MEDIA":   70,   # NCM 6 dígitos bate
    "BAIXA":    0,   # só fuzzy — score real é preenchido depois
}

SCORE_MINIMO_FUZZY = 55  # abaixo disso → SEM_MATCH


# ---------------------------------------------------------------------------
# Serialização JSON
# ---------------------------------------------------------------------------

class _JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

def conectar() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(**DB_CONFIG)


_SQL_PRODUTOS = """
    SELECT
        pf.id                          AS produto_id,
        pf.ean,
        pf.ncm,
        pf.descricao                   AS produto_descricao,
        pf.categoria,

        rt.id                          AS regra_id,
        rt.tipo_regra,
        rt.aliquota_percentual,
        rt.valor_fixo,
        rt.unidade_valor,
        rt.vigencia_inicio,
        rt.vigencia_fim,
        rt.resumo_regra,
        rt.observacoes,
        rt.ativo                       AS regra_ativa,

        t.nome                         AS tributo,
        j.tipo                         AS jurisdicao_tipo,
        j.uf,
        j.municipio,
        fl.titulo                      AS fonte_titulo,
        fl.url                         AS fonte_url

    FROM produtos_fiscais pf
    LEFT JOIN regras_tributarias rt ON rt.produto_fiscal_id = pf.id
    LEFT JOIN tributos           t  ON t.id  = rt.tributo_id
    LEFT JOIN jurisdicoes        j  ON j.id  = rt.jurisdicao_id
    LEFT JOIN fontes_legais      fl ON fl.id = rt.fonte_legal_id
    WHERE pf.ativo = 1
    ORDER BY pf.id, t.nome, j.tipo, j.uf
"""


def carregar_banco(conn) -> dict[int, dict]:
    """
    Carrega todos os produtos ativos e agrupa por produto_id.
    Retorna: { produto_id: { ean, ncm, descricao, categoria, regras: [...] } }
    """
    cur = conn.cursor(dictionary=True)
    cur.execute(_SQL_PRODUTOS)
    rows = cur.fetchall()
    cur.close()

    produtos: dict[int, dict] = {}
    for row in rows:
        pid = row["produto_id"]
        if pid not in produtos:
            produtos[pid] = {
                "produto_id": pid,
                "ean":        row["ean"],
                "ncm":        row["ncm"],
                "descricao":  row["produto_descricao"],
                "categoria":  row["categoria"],
                "regras":     [],
            }
        if row["regra_id"] is not None:
            produtos[pid]["regras"].append({
                "regra_id":            row["regra_id"],
                "tributo":             row["tributo"],
                "tipo_regra":          row["tipo_regra"],
                "aliquota_percentual": row["aliquota_percentual"],
                "valor_fixo":          row["valor_fixo"],
                "unidade_valor":       row["unidade_valor"],
                "vigencia_inicio":     row["vigencia_inicio"],
                "vigencia_fim":        row["vigencia_fim"],
                "resumo_regra":        row["resumo_regra"],
                "ativo":               bool(row["regra_ativa"]),
                "jurisdicao_tipo":     row["jurisdicao_tipo"],
                "uf":                  row["uf"],
                "municipio":           row["municipio"],
                "fonte_titulo":        row["fonte_titulo"],
                "fonte_url":           row["fonte_url"],
            })
    return produtos


# ---------------------------------------------------------------------------
# Índices para busca rápida (em memória, O(1) para EAN e NCM)
# ---------------------------------------------------------------------------

def construir_indices(produtos: dict[int, dict]) -> tuple[dict, dict, dict]:
    """
    Retorna três índices:
        idx_ean   : { ean_normalizado   → produto_id }
        idx_ncm8  : { ncm_8_digitos     → [produto_id, ...] }
        idx_ncm6  : { ncm_6_digitos     → [produto_id, ...] }
    """
    idx_ean:  dict[str, int]        = {}
    idx_ncm8: dict[str, list[int]]  = {}
    idx_ncm6: dict[str, list[int]]  = {}

    for pid, p in produtos.items():
        # EAN
        if p["ean"]:
            idx_ean[p["ean"].strip()] = pid

        # NCM (remove pontos/traços se houver)
        ncm_raw = (p["ncm"] or "").replace(".", "").replace("-", "").strip()
        if ncm_raw:
            ncm8 = ncm_raw[:8]
            ncm6 = ncm_raw[:6]
            idx_ncm8.setdefault(ncm8, []).append(pid)
            idx_ncm6.setdefault(ncm6, []).append(pid)

    return idx_ean, idx_ncm8, idx_ncm6


# ---------------------------------------------------------------------------
# Normalização para fuzzy
# ---------------------------------------------------------------------------

def _norm(texto: str) -> str:
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def score_fuzzy(desc_nfe: str, desc_banco: str) -> float:
    """
    Score combinado:
        60% token_sort_ratio  → ignora ordem das palavras
        40% partial_ratio     → match parcial (descrições diferentes em tamanho)
    """
    a = _norm(desc_nfe)
    b = _norm(desc_banco)
    return round(
        0.60 * fuzz.token_sort_ratio(a, b) +
        0.40 * fuzz.partial_ratio(a, b),
        2,
    )


# ---------------------------------------------------------------------------
# Lógica de match em cascata — retorna 1 resultado
# ---------------------------------------------------------------------------

def match_item(
    item: dict,
    produtos: dict[int, dict],
    idx_ean:  dict[str, int],
    idx_ncm8: dict[str, list[int]],
    idx_ncm6: dict[str, list[int]],
) -> dict:
    """
    Tenta casar o item da NF-e com exatamente 1 produto do banco,
    percorrendo os níveis em ordem decrescente de confiança.
    """
    ean_nfe = (item.get("codigo") or "").strip()
    ncm_nfe = (item.get("ncm")    or "").replace(".", "").replace("-", "").strip()
    desc_nfe = item.get("descricao") or ""

    # ── Nível 1: EAN exato ──────────────────────────────────────────────────
    if ean_nfe and ean_nfe in idx_ean:
        pid = idx_ean[ean_nfe]
        return _montar_resultado(
            item, produtos[pid],
            nivel="EAN_EXATO",
            confianca=CONFIANCA["EXATA"],
            score=100.0,
        )

    # ── Nível 2: NCM 8 dígitos exato ────────────────────────────────────────
    if ncm_nfe:
        ncm8_nfe = ncm_nfe[:8]
        candidatos_ncm8 = idx_ncm8.get(ncm8_nfe, [])

        if len(candidatos_ncm8) == 1:
            # match único e direto
            return _montar_resultado(
                item, produtos[candidatos_ncm8[0]],
                nivel="NCM_EXATO",
                confianca=CONFIANCA["ALTA"],
                score=90.0,
            )

        if len(candidatos_ncm8) > 1:
            # desempata pelo melhor fuzzy dentro do grupo
            melhor = _melhor_fuzzy_do_grupo(desc_nfe, candidatos_ncm8, produtos)
            return _montar_resultado(
                item, produtos[melhor["pid"]],
                nivel="NCM_EXATO_DESEMPATE_FUZZY",
                confianca=CONFIANCA["ALTA"],
                score=melhor["score"],
            )

        # ── Nível 3: NCM 6 dígitos parcial ──────────────────────────────────
        ncm6_nfe = ncm_nfe[:6]
        candidatos_ncm6 = idx_ncm6.get(ncm6_nfe, [])

        if candidatos_ncm6:
            melhor = _melhor_fuzzy_do_grupo(desc_nfe, candidatos_ncm6, produtos)
            return _montar_resultado(
                item, produtos[melhor["pid"]],
                nivel="NCM_PARCIAL",
                confianca=CONFIANCA["MEDIA"],
                score=melhor["score"],
            )

    # ── Nível 4: Fuzzy puro na descrição (varredura completa) ───────────────
    melhor_pid:   int   = -1
    melhor_score: float = 0.0

    for pid, p in produtos.items():
        s = score_fuzzy(desc_nfe, p["descricao"])
        if s > melhor_score:
            melhor_score = s
            melhor_pid   = pid

    if melhor_pid != -1 and melhor_score >= SCORE_MINIMO_FUZZY:
        return _montar_resultado(
            item, produtos[melhor_pid],
            nivel="FUZZY_DESCRICAO",
            confianca=melhor_score,      # confiança = o próprio score aqui
            score=melhor_score,
        )

    # ── Sem match ────────────────────────────────────────────────────────────
    return _montar_resultado(item, None, nivel="SEM_MATCH", confianca=0, score=0.0)


def _melhor_fuzzy_do_grupo(
    desc_nfe: str,
    pids: list[int],
    produtos: dict[int, dict],
) -> dict:
    """Dentro de uma lista de candidatos, retorna o pid com maior score fuzzy."""
    melhor = {"pid": pids[0], "score": 0.0}
    for pid in pids:
        s = score_fuzzy(desc_nfe, produtos[pid]["descricao"])
        if s > melhor["score"]:
            melhor = {"pid": pid, "score": s}
    return melhor


def _montar_resultado(
    item: dict,
    produto: dict | None,
    nivel: str,
    confianca: float,
    score: float,
) -> dict:
    """Monta o dicionário de resultado padronizado."""
    return {
        # Dados da NF-e
        "item_numero":       item["numero_item"],
        "item_ean_nfe":      item.get("codigo"),
        "item_ncm_nfe":      item.get("ncm"),
        "item_descricao_nfe":item.get("descricao"),
        "item_unidade":      item.get("unidade"),
        "item_quantidade":   item.get("quantidade"),
        "item_valor_unit":   item.get("valor_unitario"),
        "item_valor_total":  item.get("valor_total"),
        "item_impostos_nfe": item.get("impostos", {}),

        # Diagnóstico do match
        "match_nivel":      nivel,
        "match_score":      score,
        "match_confianca":  confianca,

        # Produto encontrado (ou None)
        "produto": {
            "produto_id": produto["produto_id"],
            "ean":        produto["ean"],
            "ncm":        produto["ncm"],
            "descricao":  produto["descricao"],
            "categoria":  produto["categoria"],
            "regras":     produto["regras"],
        } if produto else None,
    }


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------

def processar(xml_path: str) -> tuple[list[dict], dict]:
    log.info(f"Extraindo NF-e: {xml_path}")
    dados = extrair_nfe(xml_path)
    cabecalho    = dados["cabecalho"]
    itens_nfe    = dados["produtos"]
    log.info(f"NF-e nº {cabecalho['numero']} — {len(itens_nfe)} itens")

    log.info("Conectando ao banco...")
    conn     = conectar()
    produtos = carregar_banco(conn)
    conn.close()
    log.info(f"{len(produtos)} produtos carregados do banco")

    idx_ean, idx_ncm8, idx_ncm6 = construir_indices(produtos)
    log.info("Índices construídos (EAN, NCM8, NCM6)")

    resultados = []
    for item in itens_nfe:
        r = match_item(item, produtos, idx_ean, idx_ncm8, idx_ncm6)
        nivel = r["match_nivel"]
        score = r["match_score"]
        desc  = r["item_descricao_nfe"] or ""
        match = r["produto"]["descricao"] if r["produto"] else "—"
        log.info(f"  [{nivel:<30}] {score:5.1f}  |  '{desc[:35]}' → '{match[:35]}'")
        resultados.append(r)

    return resultados, cabecalho


# ---------------------------------------------------------------------------
# Exportação
# ---------------------------------------------------------------------------

def salvar_json(resultados: list[dict], cabecalho: dict, caminho: str):
    saida = {"cabecalho": cabecalho, "itens": resultados}
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2, cls=_JSONEncoder)
    log.info(f"JSON salvo: {caminho}")


def salvar_csv(resultados: list[dict], cabecalho: dict, caminho: str):
    linhas = []
    for r in resultados:
        p = r["produto"] or {}
        regras_ativas = sum(1 for reg in p.get("regras", []) if reg.get("ativo"))
        linhas.append({
            "nfe_numero":        cabecalho["numero"],
            "nfe_data":          cabecalho["data_emissao"],
            "item_numero":       r["item_numero"],
            "item_ean":          r["item_ean_nfe"],
            "item_ncm":          r["item_ncm_nfe"],
            "item_descricao":    r["item_descricao_nfe"],
            "item_qtd":          r["item_quantidade"],
            "item_valor_total":  r["item_valor_total"],
            "match_nivel":       r["match_nivel"],
            "match_score":       r["match_score"],
            "match_confianca":   r["match_confianca"],
            "produto_id":        p.get("produto_id"),
            "produto_ean":       p.get("ean"),
            "produto_ncm":       p.get("ncm"),
            "produto_descricao": p.get("descricao"),
            "produto_categoria": p.get("categoria"),
            "regras_ativas":     regras_ativas,
        })

    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=linhas[0].keys())
        writer.writeheader()
        writer.writerows(linhas)
    log.info(f"CSV salvo: {caminho}")


def imprimir_terminal(resultados: list[dict], cabecalho: dict):
    ICONE = {
        "EAN_EXATO":                  "🟢",
        "NCM_EXATO":                  "🟢",
        "NCM_EXATO_DESEMPATE_FUZZY":  "🟡",
        "NCM_PARCIAL":                "🟡",
        "FUZZY_DESCRICAO":            "🟠",
        "SEM_MATCH":                  "🔴",
    }
    print(f"\n{'='*100}")
    print(f"  NF-e {cabecalho['numero']}  |  Emissão: {cabecalho['data_emissao']}  |  Total: R$ {cabecalho['valor_total']}")
    print(f"{'='*100}")

    for r in resultados:
        icone = ICONE.get(r["match_nivel"], "⚪")
        nivel = r["match_nivel"]
        score = r["match_score"]
        print(f"\n{icone}  Item {r['item_numero']:>3} | {nivel:<32} | score: {score:5.1f}")
        print(f"   NF-e   : {r['item_descricao_nfe']}")

        if r["produto"]:
            p = r["produto"]
            regras_ativas = [reg for reg in p["regras"] if reg.get("ativo")]
            print(f"   Banco  : {p['descricao']}  [NCM: {p['ncm']}]")
            if regras_ativas:
                print(f"   Regras ativas ({len(regras_ativas)}):")
                for reg in regras_ativas:
                    uf  = f" {reg['uf']}" if reg.get("uf") else " FED"
                    alq = (
                        f"{float(reg['aliquota_percentual']):.2f}%"
                        if reg["aliquota_percentual"] is not None
                        else f"R$ {float(reg['valor_fixo']):.4f}/{reg['unidade_valor']}"
                    )
                    print(f"     • {reg['tributo']:<7}{uf:<4} | {reg['tipo_regra']:<25} | {alq}")
        else:
            print("   Banco  : — nenhum produto encontrado —")

    print(f"\n{'='*100}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Encontra O produto fiscal correspondente a cada item de uma NF-e."
    )
    parser.add_argument("xml",   help="Caminho do arquivo XML da NF-e")
    parser.add_argument("--out", help="Saída: resultado.json ou resultado.csv",
                        default=None)
    args = parser.parse_args()

    if not Path(args.xml).exists():
        log.error(f"Arquivo não encontrado: {args.xml}")
        sys.exit(1)

    resultados, cabecalho = processar(args.xml)
    imprimir_terminal(resultados, cabecalho)

    out = args.out
    if not out:
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = f"resultado_nfe_{cabecalho['numero']}_{ts}.json"

    if Path(out).suffix.lower() == ".csv":
        salvar_csv(resultados, cabecalho, out)
    else:
        salvar_json(resultados, cabecalho, out)


if __name__ == "__main__":
    main()