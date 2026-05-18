import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
NFE_NS = NS["nfe"]


def strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def child(element, tag):
    if element is None:
        return None
    found = element.find(f"{{{NFE_NS}}}{tag}")
    if found is None:
        found = element.find(tag)
    return found


def children(element, tag):
    if element is None:
        return []
    found = element.findall(f"{{{NFE_NS}}}{tag}")
    return found or element.findall(tag)


def find_any(root, path):
    found = root.find(path.replace("//", f"//{{{NFE_NS}}}"))
    if found is None:
        found = root.find(path)
    return found


def text(element, tag=None):
    target = child(element, tag) if tag else element
    if target is None or target.text is None:
        return None
    value = target.text.strip()
    return value if value != "" else None


def decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(Decimal(str(value).replace(",", ".")))
    except (InvalidOperation, ValueError):
        return None


def date_iso(value):
    if not value:
        return None
    raw = value.strip()
    date_part = raw.split("T", 1)[0]
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return raw


def element_to_dict(element):
    if element is None:
        return {}
    data = {}
    children_list = list(element)
    if not children_list:
        value = element.text.strip() if element.text else None
        return value
    for item in children_list:
        key = strip_ns(item.tag)
        value = element_to_dict(item)
        if key in data:
            if not isinstance(data[key], list):
                data[key] = [data[key]]
            data[key].append(value)
        else:
            data[key] = value
    return data


def extract_inf_nfe(root):
    inf = root.find(f".//{{{NFE_NS}}}infNFe")
    if inf is None:
        inf = root.find(".//infNFe")
    return inf


def uf_from_party(inf_nfe, party_tag):
    party = child(inf_nfe, party_tag)
    if party is None:
        return None
    address = child(party, "enderEmit") if party_tag == "emit" else child(party, "enderDest")
    return text(address, "UF")


def extract_header(root):
    inf_nfe = extract_inf_nfe(root)
    ide = child(inf_nfe, "ide")
    total = child(child(inf_nfe, "total"), "ICMSTot")
    tp_nf = text(ide, "tpNF")
    tipo_map = {"0": "entrada", "1": "saida"}
    dh_emi = text(ide, "dhEmi") or text(ide, "dEmi")
    inf_id = inf_nfe.attrib.get("Id") if inf_nfe is not None else None
    chave = text(root.find(f".//{{{NFE_NS}}}protNFe/{{{NFE_NS}}}infProt"), "chNFe")

    if not chave and inf_id and inf_id.startswith("NFe"):
        chave = inf_id[3:]

    return {
        "numero": text(ide, "nNF"),
        "chave_acesso": chave,
        "data_emissao": date_iso(dh_emi),
        "data_emissao_original": dh_emi,
        "natureza_operacao": text(ide, "natOp"),
        "tipo": tipo_map.get(tp_nf, tp_nf),
        "uf_emitente": uf_from_party(inf_nfe, "emit"),
        "uf_destinatario": uf_from_party(inf_nfe, "dest"),
        "valor_total": decimal_or_none(text(total, "vNF")),
    }


def extract_tax_group(imposto, tributo):
    group = child(imposto, tributo)
    if group is None:
        return None

    raw = element_to_dict(group)
    details = {}
    variant = None

    for sub in list(group):
        sub_tag = strip_ns(sub.tag)
        if list(sub):
            variant = sub_tag
            for field in list(sub):
                details[strip_ns(field.tag)] = text(field)
        else:
            details[sub_tag] = text(sub)

    cst = details.get("CST") or details.get("CSOSN")
    result = {"grupo": variant, "raw": raw}

    if tributo == "ICMS":
        result.update({
            "cst": cst,
            "csosn": details.get("CSOSN"),
            "modBC": details.get("modBC"),
            "base_calculo": decimal_or_none(details.get("vBC")),
            "aliquota_percentual": decimal_or_none(details.get("pICMS")),
            "valor": decimal_or_none(details.get("vICMS")),
            "campos": details,
        })
    elif tributo == "PIS":
        result.update({
            "cst": cst,
            "base_calculo": decimal_or_none(details.get("vBC")),
            "aliquota_percentual": decimal_or_none(details.get("pPIS")),
            "valor": decimal_or_none(details.get("vPIS")),
            "campos": details,
        })
    elif tributo == "COFINS":
        result.update({
            "cst": cst,
            "base_calculo": decimal_or_none(details.get("vBC")),
            "aliquota_percentual": decimal_or_none(details.get("pCOFINS")),
            "valor": decimal_or_none(details.get("vCOFINS")),
            "campos": details,
        })
    elif tributo == "IPI":
        result.update({
            "cst": cst,
            "cEnq": details.get("cEnq"),
            "base_calculo": decimal_or_none(details.get("vBC")),
            "aliquota_percentual": decimal_or_none(details.get("pIPI")),
            "valor": decimal_or_none(details.get("vIPI")),
            "campos": details,
        })

    return result


def extract_taxes(det):
    imposto = child(det, "imposto")
    if imposto is None:
        return {}
    taxes = {}
    for tributo in ("ICMS", "PIS", "COFINS", "IPI"):
        data = extract_tax_group(imposto, tributo)
        if data is not None:
            taxes[tributo] = data
    for group in list(imposto):
        name = strip_ns(group.tag)
        if name not in taxes and name != "vTotTrib":
            taxes[name] = {"raw": element_to_dict(group)}
    return taxes


def extract_items(root):
    dets = root.findall(f".//{{{NFE_NS}}}det")
    if not dets:
        dets = root.findall(".//det")

    items = []
    for index, det in enumerate(dets, start=1):
        prod = child(det, "prod")
        if prod is None:
            continue
        item_number = det.attrib.get("nItem") or str(index)
        items.append({
            "numero_item": int(item_number) if item_number.isdigit() else index,
            "cProd": text(prod, "cProd"),
            "cEAN": text(prod, "cEAN"),
            "cEANTrib": text(prod, "cEANTrib"),
            "xProd": text(prod, "xProd"),
            "NCM": text(prod, "NCM"),
            "CEST": text(prod, "CEST"),
            "CFOP": text(prod, "CFOP"),
            "uCom": text(prod, "uCom"),
            "qCom": decimal_or_none(text(prod, "qCom")),
            "vUnCom": decimal_or_none(text(prod, "vUnCom")),
            "vProd": decimal_or_none(text(prod, "vProd")),
            "vDesc": decimal_or_none(text(prod, "vDesc")),
            "vFrete": decimal_or_none(text(prod, "vFrete")),
            "vSeg": decimal_or_none(text(prod, "vSeg")),
            "vOutro": decimal_or_none(text(prod, "vOutro")),
            "impostos": extract_taxes(det),
            "produto_raw": element_to_dict(prod),
        })
    return items


def extrair_nfe(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    return {
        "cabecalho": extract_header(root),
        "produtos": extract_items(root),
    }


def imprimir_resumo(dados):
    cab = dados["cabecalho"]
    print("=" * 90)
    print(
        f"NF-e {cab.get('numero') or 'N/A'} | "
        f"Emissao: {cab.get('data_emissao') or 'N/A'} | "
        f"Total: R$ {cab.get('valor_total') or 0:.2f}"
    )
    print("=" * 90)
    for item in dados["produtos"]:
        print(
            f"Item {item['numero_item']}: "
            f"{item.get('NCM') or 'sem NCM'} | "
            f"{item.get('xProd') or ''} | "
            f"R$ {item.get('vProd') or 0:.2f}"
        )
        for tributo, imposto in item.get("impostos", {}).items():
            cst = imposto.get("cst") or imposto.get("csosn") or "N/A"
            valor = imposto.get("valor")
            aliquota = imposto.get("aliquota_percentual")
            print(f"  - {tributo}: CST/CSOSN {cst}, aliq {aliquota}, valor {valor}")


class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def main():
    parser = argparse.ArgumentParser(description="Extrai cabecalho, itens e impostos de XML de NF-e.")
    parser.add_argument("xml", help="Arquivo XML da NF-e")
    parser.add_argument("--json", action="store_true", help="Imprime JSON completo")
    args = parser.parse_args()

    if not Path(args.xml).exists():
        print(f"Arquivo nao encontrado: {args.xml}", file=sys.stderr)
        return 1

    dados = extrair_nfe(args.xml)
    if args.json:
        print(json.dumps(dados, ensure_ascii=False, indent=2, cls=JSONEncoder))
    else:
        imprimir_resumo(dados)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
