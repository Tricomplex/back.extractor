import argparse
import csv
import json
import logging
import os
import sys
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

try:
    import mysql.connector
except ImportError:
    mysql = None

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

from extractor import extrair_nfe


TRIBUTOS_MVP = ("ICMS", "PIS", "COFINS", "IPI")
UF_MVP = "SP"
TOLERANCIA_VALOR = 0.02
TOLERANCIA_ALIQUOTA = 0.01
SCORE_MINIMO_FUZZY = 55.0

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "tricomplex"),
    "charset": "utf8mb4",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def as_float(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_ncm(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def normalize_text(value):
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value).lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.replace("/", " ").replace("-", " ").split())


def fuzzy_score(a, b):
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if fuzz:
        return round(
            0.60 * fuzz.token_sort_ratio(a_norm, b_norm)
            + 0.40 * fuzz.partial_ratio(a_norm, b_norm),
            2,
        )

    from difflib import SequenceMatcher

    return round(SequenceMatcher(None, a_norm, b_norm).ratio() * 100, 2)


def conectar():
    if mysql is None:
        raise RuntimeError("mysql-connector-python nao esta instalado.")
    return mysql.connector.connect(**DB_CONFIG)


def carregar_produtos(conn):
    sql = """
        SELECT
            pf.id AS produto_id,
            pf.ncm,
            pf.descricao,
            pf.categoria,
            COUNT(rt.id) AS total_regras_ativas
        FROM produtos_fiscais pf
        LEFT JOIN regras_tributarias rt
            ON rt.produto_fiscal_id = pf.id
           AND rt.ativo = 1
        WHERE pf.ativo = 1
        GROUP BY pf.id, pf.ncm, pf.descricao, pf.categoria
        ORDER BY pf.ncm, pf.id
    """
    cur = conn.cursor(dictionary=True)
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()

    produtos = []
    for row in rows:
        produtos.append({
            "produto_id": row["produto_id"],
            "ncm": normalize_ncm(row.get("ncm")),
            "descricao": row.get("descricao"),
            "categoria": row.get("categoria"),
            "total_regras_ativas": int(row.get("total_regras_ativas") or 0),
        })
    return produtos


def construir_indices(produtos):
    ncm8 = {}
    ncm6 = {}
    for produto in produtos:
        ncm = normalize_ncm(produto.get("ncm"))
        if len(ncm) >= 8:
            ncm8.setdefault(ncm[:8], []).append(produto)
        if len(ncm) >= 6:
            ncm6.setdefault(ncm[:6], []).append(produto)
    return ncm8, ncm6


def melhor_por_descricao(candidatos, descricao, preferir_com_regras=False):
    scored = []
    for produto in candidatos:
        score = fuzzy_score(descricao, produto.get("descricao"))
        regras_score = produto.get("total_regras_ativas", 0) if preferir_com_regras else 0
        scored.append((regras_score, score, produto))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return (scored[0][1], scored[0][2]) if scored else (0.0, None)


def match_item(item, produtos, idx_ncm8, idx_ncm6):
    ncm = normalize_ncm(item.get("NCM"))
    descricao = item.get("xProd") or ""

    if len(ncm) >= 8:
        candidatos = idx_ncm8.get(ncm[:8], [])
        if len(candidatos) == 1:
            return {"nivel": "NCM_EXATO", "score": 100.0, "produto_fiscal": candidatos[0]}
        if len(candidatos) > 1:
            _, produto = melhor_por_descricao(candidatos, descricao, preferir_com_regras=True)
            return {"nivel": "NCM_EXATO", "score": 100.0, "produto_fiscal": produto}

    if len(ncm) >= 6:
        candidatos = idx_ncm6.get(ncm[:6], [])
        if candidatos:
            _, produto = melhor_por_descricao(candidatos, descricao, preferir_com_regras=True)
            return {"nivel": "NCM_PARCIAL", "score": 70.0, "produto_fiscal": produto}

    score, produto = melhor_por_descricao(produtos, descricao)
    if produto and score >= SCORE_MINIMO_FUZZY:
        return {"nivel": "FUZZY_DESCRICAO", "score": score, "produto_fiscal": produto}

    return {"nivel": "SEM_MATCH", "score": 0.0, "produto_fiscal": None}


def buscar_regras(conn, produto_id, tributo, data_emissao, uf=UF_MVP):
    sql = """
        SELECT
            rt.id AS regra_id,
            rt.tipo_regra,
            rt.aliquota_percentual,
            rt.valor_fixo,
            rt.unidade_valor,
            rt.vigencia_inicio,
            rt.vigencia_fim,
            rt.resumo_regra,
            rt.observacoes,
            t.nome AS tributo,
            j.tipo AS jurisdicao_tipo,
            j.uf,
            j.municipio,
            fl.tipo AS fonte_tipo,
            fl.titulo AS fonte_titulo,
            fl.url AS fonte_url,
            fl.orgao AS fonte_orgao,
            fl.data_publicacao AS fonte_data_publicacao,
            fl.texto_relevante AS fonte_texto_relevante
        FROM regras_tributarias rt
        JOIN tributos t ON t.id = rt.tributo_id
        LEFT JOIN jurisdicoes j ON j.id = rt.jurisdicao_id
        LEFT JOIN fontes_legais fl ON fl.id = rt.fonte_legal_id
        WHERE rt.produto_fiscal_id = %s
          AND UPPER(t.nome) = %s
          AND rt.ativo = 1
          AND rt.vigencia_inicio <= %s
          AND (rt.vigencia_fim IS NULL OR rt.vigencia_fim >= %s)
          AND (
                j.id IS NULL
                OR UPPER(j.tipo) = 'FEDERAL'
                OR (UPPER(j.tipo) = 'ESTADUAL' AND j.uf = %s)
          )
        ORDER BY
          CASE
            WHEN UPPER(j.tipo) = 'ESTADUAL' AND j.uf = %s THEN 0
            WHEN UPPER(j.tipo) = 'FEDERAL' THEN 1
            ELSE 2
          END,
          rt.vigencia_inicio DESC,
          rt.id DESC
    """
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, (produto_id, tributo.upper(), data_emissao, data_emissao, uf, uf))
    rows = cur.fetchall()
    cur.close()
    return [normalizar_regra(row) for row in rows]


def normalizar_regra(row):
    return {
        "regra_id": row.get("regra_id"),
        "tributo": row.get("tributo"),
        "tipo_regra": row.get("tipo_regra"),
        "aliquota_percentual": as_float(row.get("aliquota_percentual")),
        "valor_fixo": as_float(row.get("valor_fixo")),
        "unidade_valor": row.get("unidade_valor"),
        "vigencia_inicio": row.get("vigencia_inicio"),
        "vigencia_fim": row.get("vigencia_fim"),
        "resumo_regra": row.get("resumo_regra"),
        "observacoes": row.get("observacoes"),
        "jurisdicao_tipo": row.get("jurisdicao_tipo"),
        "uf": row.get("uf"),
        "municipio": row.get("municipio"),
        "fonte": {
            "tipo": row.get("fonte_tipo"),
            "titulo": row.get("fonte_titulo"),
            "url": row.get("fonte_url"),
            "orgao": row.get("fonte_orgao"),
            "data_publicacao": row.get("fonte_data_publicacao"),
            "texto_relevante": row.get("fonte_texto_relevante"),
        },
    }


def texto_regra(regra):
    partes = [
        regra.get("resumo_regra"),
        regra.get("observacoes"),
        regra.get("fonte", {}).get("texto_relevante"),
        regra.get("fonte", {}).get("titulo"),
    ]
    return " ".join(str(p) for p in partes if p)


def escolher_regra(tributo, regras, descricao_item):
    if not regras:
        return None, "sem_regra", None
    if len(regras) == 1:
        return regras[0], None, None

    assinaturas = {
        (
            regra.get("tipo_regra"),
            regra.get("aliquota_percentual"),
            regra.get("valor_fixo"),
            regra.get("unidade_valor"),
        )
        for regra in regras
    }
    if len(assinaturas) == 1:
        return regras[0], None, None

    if tributo == "IPI":
        scored = [(fuzzy_score(descricao_item, texto_regra(regra)), regra) for regra in regras]
        scored.sort(key=lambda item: item[0], reverse=True)
        top_score, top_regra = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if top_score >= SCORE_MINIMO_FUZZY and top_score - second_score >= 8:
            return top_regra, None, None

    return None, "revisao_manual", "Mais de uma regra vigente aplicavel; revisar manualmente."


def imposto_declarado(item, tributo):
    imposto = item.get("impostos", {}).get(tributo)
    if not imposto:
        return None
    return {
        "cst": imposto.get("cst") or imposto.get("csosn"),
        "base_calculo": imposto.get("base_calculo"),
        "aliquota_percentual": imposto.get("aliquota_percentual"),
        "valor": imposto.get("valor"),
        "raw": imposto.get("raw"),
    }


def fonte_vazia():
    return {"titulo": None, "url": None, "texto_relevante": None}


def analise_base(tributo, status, declarado=None, esperado=None, diferenca=None, fonte=None, mensagem=""):
    return {
        "tributo": tributo,
        "status": status,
        "declarado": declarado or {
            "cst": None,
            "base_calculo": None,
            "aliquota_percentual": None,
            "valor": None,
        },
        "esperado": esperado or {
            "tipo_regra": None,
            "aliquota_percentual": None,
            "valor": None,
            "vigencia_inicio": None,
            "vigencia_fim": None,
        },
        "diferenca": diferenca or {
            "aliquota_pontos_percentuais": None,
            "valor": None,
        },
        "fonte": fonte or fonte_vazia(),
        "mensagem": mensagem,
    }


def montar_esperado(regra, valor_esperado):
    return {
        "tipo_regra": regra.get("tipo_regra"),
        "aliquota_percentual": regra.get("aliquota_percentual"),
        "valor": valor_esperado,
        "vigencia_inicio": regra.get("vigencia_inicio"),
        "vigencia_fim": regra.get("vigencia_fim"),
    }


def montar_fonte(regra):
    fonte = regra.get("fonte") or {}
    return {
        "titulo": fonte.get("titulo"),
        "url": fonte.get("url"),
        "texto_relevante": fonte.get("texto_relevante"),
    }


def comparar_tributo(tributo, item, match, regras):
    declarado = imposto_declarado(item, tributo)
    produto = match.get("produto_fiscal")

    if not declarado:
        return analise_base(
            tributo,
            "sem_imposto_na_nfe",
            mensagem=f"{tributo} nao foi declarado no item da NF-e.",
        )

    if not produto:
        return analise_base(
            tributo,
            "sem_regra",
            declarado=declarado,
            mensagem="Produto fiscal nao encontrado na base; nao ha regra aplicavel.",
        )

    regra, status_escolha, motivo = escolher_regra(tributo, regras, item.get("xProd") or "")
    if status_escolha == "sem_regra":
        return analise_base(
            tributo,
            "sem_regra",
            declarado=declarado,
            mensagem=f"Nao ha regra vigente de {tributo} para o produto fiscal encontrado.",
        )
    if status_escolha == "revisao_manual":
        return analise_base(
            tributo,
            "revisao_manual",
            declarado=declarado,
            mensagem=motivo,
        )

    tipo_regra = (regra.get("tipo_regra") or "").upper()
    aliquota_esperada = regra.get("aliquota_percentual")
    base = declarado.get("base_calculo")
    valor_declarado = declarado.get("valor")
    valor_esperado = None

    if tipo_regra == "ALIQUOTA" and base is not None and aliquota_esperada is not None:
        valor_esperado = round(base * aliquota_esperada / 100, 2)

    esperado = montar_esperado(regra, valor_esperado)
    fonte = montar_fonte(regra)

    if tipo_regra == "NAO_TRIBUTADO":
        valor_ok = valor_declarado in (None, 0) or abs(valor_declarado) <= TOLERANCIA_VALOR
        aliq_decl = declarado.get("aliquota_percentual")
        aliq_ok = aliq_decl in (None, 0) or abs(aliq_decl) <= TOLERANCIA_ALIQUOTA
        status = "ok" if valor_ok and aliq_ok else "divergente"
        msg = "Imposto tratado como nao tributado." if status == "ok" else "Regra indica nao tributado, mas ha imposto positivo na NF-e."
        return analise_base(tributo, status, declarado, esperado, fonte=fonte, mensagem=msg)

    if tipo_regra != "ALIQUOTA":
        return analise_base(
            tributo,
            "revisao_manual",
            declarado,
            esperado,
            fonte=fonte,
            mensagem=f"Tipo de regra {tipo_regra or 'desconhecido'} exige revisao manual no MVP.",
        )

    diff_aliq = None
    diff_valor = None
    aliquota_declarada = declarado.get("aliquota_percentual")
    aliquota_ok = True
    valor_ok = True

    if aliquota_esperada is not None and aliquota_declarada is not None:
        diff_aliq = round(aliquota_declarada - aliquota_esperada, 4)
        aliquota_ok = abs(diff_aliq) <= TOLERANCIA_ALIQUOTA

    if valor_esperado is not None and valor_declarado is not None:
        diff_valor = round(valor_declarado - valor_esperado, 2)
        valor_ok = abs(diff_valor) <= TOLERANCIA_VALOR

    status = "ok" if aliquota_ok and valor_ok else "divergente"
    mensagem = (
        f"{tributo} confere com a regra vigente."
        if status == "ok"
        else f"{tributo} diverge da regra vigente encontrada."
    )
    return analise_base(
        tributo,
        status,
        declarado,
        esperado,
        {
            "aliquota_pontos_percentuais": diff_aliq,
            "valor": diff_valor,
        },
        fonte,
        mensagem,
    )


def produto_nfe(item):
    return {
        "cProd": item.get("cProd"),
        "cEAN": item.get("cEAN"),
        "cEANTrib": item.get("cEANTrib"),
        "xProd": item.get("xProd"),
        "NCM": item.get("NCM"),
        "CEST": item.get("CEST"),
        "CFOP": item.get("CFOP"),
        "uCom": item.get("uCom"),
        "qCom": item.get("qCom"),
        "vUnCom": item.get("vUnCom"),
        "vProd": item.get("vProd"),
        "vDesc": item.get("vDesc"),
        "vFrete": item.get("vFrete"),
        "vSeg": item.get("vSeg"),
        "vOutro": item.get("vOutro"),
        "impostos_raw": item.get("impostos"),
    }


def resumo(itens):
    analises = [analise for item in itens for analise in item["analises"]]
    return {
        "total_itens": len(itens),
        "total_alertas": sum(1 for a in analises if a["status"] in {"divergente", "revisao_manual", "sem_regra"}),
        "total_divergencias": sum(1 for a in analises if a["status"] == "divergente"),
        "total_ok": sum(1 for a in analises if a["status"] == "ok"),
        "total_revisao_manual": sum(1 for a in analises if a["status"] == "revisao_manual"),
    }


def processar(xml_path):
    dados = extrair_nfe(xml_path)
    cabecalho = dados["cabecalho"]
    data_emissao = cabecalho.get("data_emissao") or date.today().isoformat()
    avisos = []

    produtos = []
    conn = None
    try:
        conn = conectar()
        produtos = carregar_produtos(conn)
        log.info("%s produtos fiscais carregados do banco.", len(produtos))
    except Exception as exc:
        avisos.append(f"Banco indisponivel: {exc}. A extracao XML foi validada, mas regras nao foram consultadas.")
        log.warning(avisos[-1])

    idx_ncm8, idx_ncm6 = construir_indices(produtos)
    itens_saida = []

    for item in dados["produtos"]:
        match = match_item(item, produtos, idx_ncm8, idx_ncm6) if produtos else {
            "nivel": "SEM_MATCH",
            "score": 0.0,
            "produto_fiscal": None,
        }
        analises = []
        for tributo in TRIBUTOS_MVP:
            regras = []
            produto = match.get("produto_fiscal")
            if conn is not None and produto:
                regras = buscar_regras(conn, produto["produto_id"], tributo, data_emissao, UF_MVP)
            analises.append(comparar_tributo(tributo, item, match, regras))

        itens_saida.append({
            "numero_item": item["numero_item"],
            "produto_nfe": produto_nfe(item),
            "match": match,
            "analises": analises,
        })

    if conn is not None:
        conn.close()

    saida = {
        "cabecalho": cabecalho,
        "resumo": resumo(itens_saida),
        "itens": itens_saida,
    }
    if avisos:
        saida["avisos"] = avisos
    return saida


def salvar_json(resultado, caminho):
    with open(caminho, "w", encoding="utf-8") as file:
        json.dump(resultado, file, ensure_ascii=False, indent=2, cls=JSONEncoder)


def salvar_csv(resultado, caminho):
    rows = []
    for item in resultado["itens"]:
        produto = item["produto_nfe"]
        match_produto = item["match"].get("produto_fiscal") or {}
        for analise in item["analises"]:
            rows.append({
                "nfe_numero": resultado["cabecalho"].get("numero"),
                "data_emissao": resultado["cabecalho"].get("data_emissao"),
                "item": item["numero_item"],
                "ncm": produto.get("NCM"),
                "descricao": produto.get("xProd"),
                "tributo": analise["tributo"],
                "status": analise["status"],
                "declarado_aliquota": analise["declarado"].get("aliquota_percentual"),
                "declarado_valor": analise["declarado"].get("valor"),
                "esperado_tipo": analise["esperado"].get("tipo_regra"),
                "esperado_aliquota": analise["esperado"].get("aliquota_percentual"),
                "esperado_valor": analise["esperado"].get("valor"),
                "diferenca_valor": analise["diferenca"].get("valor"),
                "match_nivel": item["match"].get("nivel"),
                "match_score": item["match"].get("score"),
                "produto_fiscal_id": match_produto.get("produto_id"),
                "produto_fiscal_ncm": match_produto.get("ncm"),
                "fonte_titulo": analise["fonte"].get("titulo"),
                "fonte_url": analise["fonte"].get("url"),
                "mensagem": analise["mensagem"],
            })

    if not rows:
        rows.append({"mensagem": "sem itens"})

    with open(caminho, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def imprimir_terminal(resultado):
    cab = resultado["cabecalho"]
    print("=" * 100)
    print(
        f"NF-e {cab.get('numero') or 'N/A'} | "
        f"Emissao {cab.get('data_emissao') or 'N/A'} | "
        f"Total R$ {cab.get('valor_total') or 0:.2f}"
    )
    print("=" * 100)
    for aviso in resultado.get("avisos", []):
        print(f"AVISO: {aviso}")
    for item in resultado["itens"]:
        produto = item["produto_nfe"]
        match = item["match"]
        fiscal = match.get("produto_fiscal") or {}
        print(
            f"Item {item['numero_item']} | {produto.get('NCM') or 'sem NCM'} | "
            f"{produto.get('xProd') or ''}"
        )
        print(
            f"  Match: {match.get('nivel')} ({match.get('score')})"
            f" -> {fiscal.get('descricao') or 'sem produto fiscal'}"
        )
        for analise in item["analises"]:
            print(f"  - {analise['tributo']}: {analise['status']} | {analise['mensagem']}")
    print("=" * 100)
    print(json.dumps(resultado["resumo"], ensure_ascii=False, cls=JSONEncoder))


def main():
    parser = argparse.ArgumentParser(description="Analisa XML de NF-e contra regras tributarias do MySQL.")
    parser.add_argument("xml", help="Arquivo XML da NF-e")
    parser.add_argument("--out", help="Arquivo .json ou .csv de saida")
    args = parser.parse_args()

    if not Path(args.xml).exists():
        print(f"Arquivo nao encontrado: {args.xml}", file=sys.stderr)
        return 1

    resultado = processar(args.xml)
    imprimir_terminal(resultado)

    if args.out:
        if Path(args.out).suffix.lower() == ".csv":
            salvar_csv(resultado, args.out)
        else:
            salvar_json(resultado, args.out)
        log.info("Resultado salvo em %s", args.out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
