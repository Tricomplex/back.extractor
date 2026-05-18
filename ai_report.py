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

Responda exclusivamente com JSON valido, sem Markdown, sem bloco ``` e sem texto
fora do JSON. O front usara esse JSON para montar a interface.

Formato obrigatorio:
{
  "titulo": "string curta",
  "resumo_executivo": "string curta e amigavel",
  "pontos_atencao": [
    {
      "item": 1,
      "tributo": "IPI",
      "status": "divergente|sem_regra|revisao_manual",
      "mensagem": "string objetiva",
      "declarado": "string ou null",
      "correto": "string ou null",
      "diferenca": "string ou null",
      "fonte": "string ou null"
    }
  ],
  "itens": [
    {
      "numero_item": 1,
      "titulo": "Produto - NCM",
      "resumo": "string curta",
      "tributos": [
        {
          "tributo": "ICMS",
          "status": "ok|divergente|sem_regra|sem_imposto_na_nfe|revisao_manual",
          "explicacao": "string amigavel",
          "declarado": "string ou null",
          "correto": "string ou null",
          "diferenca": "string ou null",
          "fonte": "string ou null"
        }
      ]
    }
  ],
  "fontes": [
    { "titulo": "string", "url": "string ou null" }
  ],
  "observacao": "string curta"
}

Inclua em pontos_atencao apenas divergencias, revisoes manuais e itens sem regra.
Para divergencias, preencha declarado, correto, diferenca e fonte quando esses
campos existirem no JSON de entrada.
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


def _parse_json_response(texto):
    cleaned = texto.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini nao retornou JSON valido: {texto}") from exc


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
                            "Transforme o JSON abaixo em um relatorio JSON amigavel, "
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

    return _parse_json_response(_extrair_texto_gemini(response_json))
