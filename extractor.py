import xml.etree.ElementTree as ET
import json
import csv
import sys
from datetime import datetime

# Namespace padrão da NF-e
NS = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}

def get_text(element, tag):
    """Extrai o texto de uma tag lidando com namespace de forma robusta."""
    if element is None:
        return None
    # Busca explícita para evitar DeprecationWarning
    el = element.find(f'{{{NS["nfe"]}}}{tag}')
    if el is None:
        el = element.find(tag)
    
    return el.text.strip() if el is not None and el.text is not None else None

def fmt_date(date_str):
    """Formata data ISO para dd/mm/yyyy."""
    if not date_str:
        return None
    date_part = date_str.split('T')[0]
    try:
        dt = datetime.strptime(date_part, '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except (ValueError, IndexError):
        return date_str

def extrair_detalhes_imposto(det_element):
    """Extrai dinamicamente qualquer imposto (ICMS, PIS, COFINS, IPI) do item."""
    impostos = {}
    imposto_tag = det_element.find(f'{{{NS["nfe"]}}}imposto')
    if imposto_tag is None:
        imposto_tag = det_element.find('.//imposto')
    
    if imposto_tag is not None:
        for tributo in imposto_tag:
            nome_tributo = tributo.tag.replace(f'{{{NS["nfe"]}}}', '')
            for sub_grupo in tributo:
                dados = {'tipo': sub_grupo.tag.replace(f'{{{NS["nfe"]}}}', '')}
                for campo in sub_grupo:
                    tag_campo = campo.tag.replace(f'{{{NS["nfe"]}}}', '')
                    dados[tag_campo] = campo.text
                impostos[nome_tributo] = dados
    return impostos

def extrair_nfe(xml_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()

    def find_in_root(tag):
        """Busca elemento no root evitando DeprecationWarning."""
        el = root.find(f'.//{{{NS["nfe"]}}}{tag}')
        if el is None:
            el = root.find(f'.//{tag}')
        return el

    # --- Cabeçalho ---
    tp_nf_el = find_in_root('tpNF')
    tp_nf = tp_nf_el.text.strip() if tp_nf_el is not None else None
    tipo_map = {'0': 'Entrada', '1': 'Saída'}

    dh_emi = find_in_root('dhEmi')
    if dh_emi is None:
        dh_emi = find_in_root('dEmi')
    data_str = dh_emi.text.strip() if dh_emi is not None else None

    # Uso de getattr com default para evitar erros caso a tag não exista
    cabecalho = {
        'numero': getattr(find_in_root('nNF'), 'text', 'N/A'),
        'data_emissao': fmt_date(data_str),
        'natureza_operacao': getattr(find_in_root('natOp'), 'text', 'N/A'),
        'tipo': tipo_map.get(tp_nf, tp_nf),
        'chave_acesso': getattr(find_in_root('chNFe'), 'text', 'N/A'),
        'valor_total': getattr(find_in_root('vNF'), 'text', '0.00'),
    }

    # --- Produtos ---
    produtos = []
    # Busca a lista de itens
    dets = root.findall(f'.//{{{NS["nfe"]}}}det')
    if not dets:
        dets = root.findall('.//det')

    for i, det in enumerate(dets, start=1):
        # Correção do Warning: verificação separada em vez de usar 'or'
        prod = det.find(f'{{{NS["nfe"]}}}prod')
        if prod is None:
            prod = det.find('prod')
        
        if prod is not None:
            item = {
                'numero_item': i,
                'codigo': get_text(prod, 'cProd'),
                'descricao': get_text(prod, 'xProd'),
                'unidade': get_text(prod, 'uCom'),
                'quantidade': get_text(prod, 'qCom'),
                'valor_unitario': get_text(prod, 'vUnCom'),
                'valor_total': get_text(prod, 'vProd'),
                'impostos': extrair_detalhes_imposto(det)
            }
            produtos.append(item)

    return {'cabecalho': cabecalho, 'produtos': produtos}

def imprimir_resumo(dados):
    cab = dados['cabecalho']
    print('\n' + '='*90)
    print(f"NF-e: {cab['numero']} | Emissão: {cab['data_emissao']} | Total: R$ {cab['valor_total']}")
    print('='*90)
    
    for p in dados['produtos']:
        # Mostra o código do produto entre colchetes
        codigo_display = p['codigo'] if p['codigo'] else "S/ COD"
        print(f"Item {p['numero_item']}: [{codigo_display}] {p['descricao'][:40]:<40} | Qtd: {p['quantidade']:>6}")
        
        for imp, detalhes in p['impostos'].items():
            # Tenta pegar CST ou CSOSN (Simples Nacional)
            cst = detalhes.get('CST') or detalhes.get('CSOSN') or 'N/A'
            # Busca o valor do imposto em diferentes tags possíveis
            v_imp = detalhes.get('vICMS') or detalhes.get('vPIS') or detalhes.get('vCOFINS') or detalhes.get('vIPI') or '0.00'
            print(f"   └─ {imp:<7} [CST: {cst:>3}] | Valor: R$ {v_imp:>10}")
        print('-' * 90)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Uso: python extrator_nfe.py <arquivo.xml>')
        sys.exit(1)

    xml_path = sys.argv[1]
    try:
        dados = extrair_nfe(xml_path)
        imprimir_resumo(dados)
    except Exception as e:
        print(f"Erro ao processar o arquivo: {e}")