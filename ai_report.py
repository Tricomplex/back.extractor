import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from decimal import Decimal


class SafeJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _compactar_resultado(resultado):
    itens = []
    for item in resultado.get("itens", []):
        produto = item.get("produto_nfe", {})
        match = item.get("match", {})
        produto_fiscal = match.get("produto_fiscal") or {}
        analises = []

        for analise in item.get("analises", []):
            analises.append({
                "tributo": analise.get("tributo"),
                "status": analise.get("status"),
                "declarado": {
                    "cst": analise.get("declarado", {}).get("cst"),
                    "base_calculo": analise.get("declarado", {}).get("base_calculo"),
                    "aliquota_percentual": analise.get("declarado", {}).get("aliquota_percentual"),
                    "valor": analise.get("declarado", {}).get("valor"),
                },
                "esperado": analise.get("esperado"),
                "diferenca": analise.get("diferenca"),
                "fonte": analise.get("fonte"),
                "mensagem": analise.get("mensagem"),
            })

        itens.append({
            "numero_item": item.get("numero_item"),
            "produto_nfe": {
                "cProd": produto.get("cProd"),
                "xProd": produto.get("xProd"),
                "NCM": produto.get("NCM"),
                "CFOP": produto.get("CFOP"),
                "qCom": produto.get("qCom"),
                "vProd": produto.get("vProd"),
            },
            "match": {
                "nivel": match.get("nivel"),
                "score": match.get("score"),
                "produto_fiscal": {
                    "produto_id": produto_fiscal.get("produto_id"),
                    "ncm": produto_fiscal.get("ncm"),
                    "descricao": produto_fiscal.get("descricao"),
                    "categoria": produto_fiscal.get("categoria"),
                },
            },
            "analises": analises,
        })

    return {
        "cabecalho": resultado.get("cabecalho"),
        "resumo": resultado.get("resumo"),
        "avisos": resultado.get("avisos", []),
        "itens": itens,
    }


def _instrucoes():
    return """
Voce e uma camada de apresentacao do Tricomplex para contadores.
Recebera um JSON ja calculado por regras deterministicas. Nao altere status,
valores, aliquotas, fontes, diferencas ou conclusoes. Nao invente regra,
fundamento legal ou risco.

Responda em portugues do Brasil, em Markdown de chat, com linguagem clara e
profissional. Seja completo, mas direto.

Formato obrigatorio:

## Analise da NF-e
Paragrafo curto com numero da nota, data, total e quantidade de itens.

## Resumo
Bullets com total de OK, divergencias, revisao manual e sem regra quando houver.

## Pontos de atencao
Liste somente divergencias, revisoes manuais e itens sem regra. Se nao houver,
diga que nao foram encontrados alertas relevantes.
Para cada divergencia, informe obrigatoriamente:
- o que foi declarado na NF-e;
- o que seria o correto segundo o JSON;
- a diferenca;
- a fonte legal/titulo/URL quando houver.

## Itens analisados
Para cada item, informe produto, NCM, match e uma lista de tributos com status,
declarado, esperado, diferenca quando existir e mensagem objetiva.
Em tributos divergentes, destaque "Declarado" e "Correto esperado" de forma
comparavel. Nunca diga apenas que divergiu.

## Fontes utilizadas
Liste as fontes legais que aparecerem no JSON. Se uma analise estiver sem fonte,
nao invente.

## Observacao
Inclua uma frase dizendo que a analise e automatizada e deve ser revisada por um
contador antes de qualquer tomada de decisao fiscal.
""".strip()


def _gemini_url(base_url, model, api_key):
    base = base_url.rstrip("/")
    model_path = model if model.startswith("models/") else f"models/{model}"
    model_path = urllib.parse.quote(model_path, safe="/")
    return f"{base}/{model_path}:generateContent?key={urllib.parse.quote(api_key)}"


def _extrair_texto_gemini(response_json):
    partes = []
    for candidate in response_json.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            text = part.get("text")
            if text:
                partes.append(text)
    texto = "\n".join(partes).strip()
    if not texto:
        raise RuntimeError(f"Gemini nao retornou texto. Resposta: {json.dumps(response_json, ensure_ascii=False)}")
    return texto


def gerar_resposta_amigavel(resultado, model=None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY nao configurada.")

    model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
    timeout = int(os.getenv("GEMINI_TIMEOUT", "60"))
    payload = _compactar_resultado(resultado)

    request_body = {
        "systemInstruction": {
            "parts": [{"text": _instrucoes()}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Transforme o JSON abaixo em uma resposta em Markdown, "
                            "respeitando estritamente as instrucoes do sistema.\n\n"
                            f"{json.dumps(payload, ensure_ascii=False, cls=SafeJSONEncoder)}"
                        ),
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
        },
    }

    data = json.dumps(request_body, ensure_ascii=False, cls=SafeJSONEncoder).encode("utf-8")
    request = urllib.request.Request(
        _gemini_url(base_url, model, api_key),
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini retornou HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Nao foi possivel conectar ao Gemini: {exc}") from exc

    return _extrair_texto_gemini(response_json)
