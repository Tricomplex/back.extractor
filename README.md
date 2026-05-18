# back.extractor

Motor MVP de extracao e analise de XML de NF-e para o Tricomplex.

O objetivo deste modulo e ler o XML da NF-e, extrair cabecalho, itens, NCMs e impostos declarados, casar cada item com a base fiscal no MySQL e comparar o declarado contra regras tributarias vigentes. O motor e deterministico: nao usa LLM para escolher regra, nao inventa regra ausente e marca ambiguidade como `revisao_manual`.

## Escopo do MVP

- UF principal: SP.
- Tributos analisados: ICMS, PIS, COFINS e IPI.
- Match de produto:
  - NCM exato de 8 digitos.
  - NCM parcial de 6 digitos como fallback.
  - Similaridade por descricao somente como fallback ou desempate.
- DIFAL e ICMS-ST podem aparecer nos XMLs, mas ficam fora da decisao automatica enquanto nao houver regra segura aplicavel.

## Variaveis de ambiente

O modulo le `.env` quando `python-dotenv` estiver instalado. Variaveis aceitas:

```text
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=tricomplex

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
CORS_ORIGINS=*
```

Dependencias esperadas para consulta ao banco:

```bash
pip install -r requirements.txt
```

Sem `rapidfuzz`, o matcher usa fallback simples com `difflib`. Sem MySQL acessivel, a extracao XML continua funcionando e o resultado informa que as regras nao foram consultadas.
Sem `GEMINI_API_KEY`, a analise deterministica continua funcionando; apenas a resposta amigavel com IA fica indisponivel.

## Uso

Extrair somente os dados do XML:

```bash
python extractor.py nota_exemplo.xml
python extractor.py nota_exemplo.xml --json
```

Analisar a nota contra o banco:

```bash
python matcher.py nota_exemplo.xml
```

Salvar JSON completo:

```bash
python matcher.py nota_exemplo.xml --out resultado.json
```

Salvar CSV resumido por item e tributo:

```bash
python matcher.py nota_exemplo.xml --out resultado.csv
```

Gerar tambem uma resposta amigavel em Markdown usando IA:

```bash
python matcher.py nota_exemplo.xml --ai
```

Usar outro modelo Gemini sem alterar o `.env`:

```bash
python matcher.py nota_exemplo.xml --ai --ai-model gemini-2.5-flash-lite
```

Salvar JSON completo com a resposta amigavel embutida:

```bash
python matcher.py nota_exemplo.xml --ai --out resultado.json
```

Salvar a resposta amigavel em um arquivo separado:

```bash
python matcher.py nota_exemplo.xml --ai --ai-out resposta.md
```

## API FastAPI

Subir a API local:

```bash
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Verificar saude:

```bash
curl http://localhost:8000/health
```

Enviar um XML e receber Markdown gerado pela IA:

```bash
curl -X POST http://localhost:8000/analisar-nfe \
  -F "file=@nota_exemplo.xml"
```

No PowerShell:

```powershell
Invoke-RestMethod -Uri http://localhost:8000/analisar-nfe -Method Post -Form @{ file = Get-Item .\nota_exemplo.xml }
```

Endpoint de debug sem IA, retornando o JSON deterministico:

```bash
curl -X POST http://localhost:8000/analisar-nfe-json \
  -F "file=@nota_exemplo.xml"
```

## Saida JSON

Formato resumido:

```json
{
  "cabecalho": {
    "numero": "123",
    "chave_acesso": "3524...",
    "data_emissao": "2024-05-15",
    "natureza_operacao": "Venda de Mercadoria",
    "tipo": "saida",
    "uf_emitente": "SP",
    "uf_destinatario": "SP",
    "valor_total": 4109.6
  },
  "resumo": {
    "total_itens": 3,
    "total_alertas": 0,
    "total_divergencias": 0,
    "total_ok": 0,
    "total_revisao_manual": 0
  },
  "itens": [
    {
      "numero_item": 1,
      "produto_nfe": {
        "cProd": "PROD001",
        "cEAN": "789...",
        "xProd": "Produto",
        "NCM": "22021000",
        "CFOP": "5102",
        "impostos_raw": {}
      },
      "match": {
        "nivel": "NCM_EXATO",
        "score": 100,
        "produto_fiscal": {}
      },
      "analises": [
        {
          "tributo": "ICMS",
          "status": "ok",
          "declarado": {},
          "esperado": {},
          "diferenca": {},
          "fonte": {},
          "mensagem": "ICMS confere com a regra vigente."
        }
      ]
    }
  ],
  "resposta_amigavel": "## Analise da NF-e\n..."
}
```

Status possiveis por tributo:

- `ok`: declarado esta dentro da tolerancia.
- `divergente`: aliquota ou valor calculado diverge da regra.
- `sem_regra`: nao ha regra vigente aplicavel no banco.
- `sem_imposto_na_nfe`: o tributo nao aparece no item do XML.
- `revisao_manual`: ha ambiguidade ou tipo de regra ainda nao automatizado no MVP.

## Limitacoes atuais

- O schema atual nao possui `ean` em `produtos_fiscais`; por isso o matcher nao depende de EAN.
- Regra estadual usa SP no MVP.
- Regras de tipo diferente de `ALIQUOTA` e `NAO_TRIBUTADO` sao encaminhadas para revisao manual.
- Quando ha multiplas regras de IPI para o mesmo produto, o motor tenta desempatar por descricao; se continuar ambiguo, retorna `revisao_manual`.
- A qualidade da analise depende das regras vigentes ja carregadas pelo scraper no MySQL.
- A IA Gemini e usada somente para formatar a resposta em Markdown. Ela recebe o JSON ja calculado e nao deve alterar regra, status, aliquota, valor, fonte ou conclusao.
