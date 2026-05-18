# back.extractor

Motor de extracao e analise de XML de NF-e do Tricomplex.

Este modulo recebe XML de NF-e, extrai cabecalho, itens, NCMs e impostos declarados, busca regras tributarias vigentes no MySQL, compara declarado vs esperado e expoe o resultado via CLI e API FastAPI.

A decisao fiscal e deterministica. Gemini e usado apenas para transformar o resultado calculado em textos amigaveis, retornando JSON estruturado para o front renderizar.

## Papel na arquitetura

```text
front React
  -> POST /analisar-nfe
  -> back/extractor
       -> extractor.py le XML
       -> matcher.py compara com regras MySQL
       -> ai_report.py pede texto amigavel ao Gemini
  -> JSON { dados, relatorio }
```

O extractor nao popula banco. Quem popula o MySQL e o `back/scraper`.

## Arquivos principais

- `extractor.py`: parser de XML de NF-e.
- `matcher.py`: match fiscal, busca de regras e comparacao declarado vs esperado.
- `ai_report.py`: camada Gemini para gerar relatorio amigavel em JSON.
- `api.py`: API FastAPI usada pelo front.
- `nota_exemplo.xml`: exemplo com bebidas do escopo MVP.
- `nota_exemplo2.xml`: exemplo com impressora, toner, papel e alimento.
- `requirements.txt`: dependencias Python do modulo.

## Escopo do MVP

- UF principal: SP.
- Tributos analisados: ICMS, PIS, COFINS e IPI.
- Match de produto:
  - NCM exato de 8 digitos.
  - NCM parcial de 6 digitos como fallback.
  - Fuzzy de descricao apenas como fallback/desempate.
- DIFAL e ICMS-ST nao sao automatizados sem regra segura aplicavel.

## Variaveis de ambiente

Crie um `.env` a partir de `.env.example`.

```text
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=tricomplex

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
GEMINI_TIMEOUT=60

CORS_ORIGINS=*
```

Notas:

- `GEMINI_API_KEY` e obrigatoria para `/analisar-nfe`, porque esse endpoint inclui relatorio amigavel.
- `/analisar-nfe-json` nao usa Gemini; serve para debug do motor deterministico.
- `CORS_ORIGINS=*` e util localmente. Em deploy, prefira a URL real do front.

## Instalar

```bash
pip install -r requirements.txt
```

## Uso via CLI

Extrair somente o XML:

```bash
python extractor.py nota_exemplo.xml
python extractor.py nota_exemplo.xml --json
```

Rodar analise deterministica contra o banco:

```bash
python matcher.py nota_exemplo.xml
```

Salvar JSON completo:

```bash
python matcher.py nota_exemplo.xml --out resultado.json
```

Salvar CSV resumido por item/tributo:

```bash
python matcher.py nota_exemplo.xml --out resultado.csv
```

Gerar tambem resposta amigavel via Gemini:

```bash
python matcher.py nota_exemplo.xml --ai
```

Salvar a resposta amigavel separada:

```bash
python matcher.py nota_exemplo.xml --ai --ai-out resposta.json
```

## Uso via API

Entre na pasta do extractor:

```bash
cd back/extractor
```

Suba a API:

```bash
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Importante: rode esse comando dentro de `back/extractor`. Se rodar na raiz do repo principal, o Uvicorn nao encontra `api.py`.

Health check:

```bash
curl http://localhost:8000/health
```

Enviar XML e receber resposta completa:

```bash
curl -X POST http://localhost:8000/analisar-nfe \
  -F "file=@nota_exemplo.xml"
```

No PowerShell:

```powershell
Invoke-RestMethod -Uri http://localhost:8000/analisar-nfe -Method Post -Form @{ file = Get-Item .\nota_exemplo.xml }
```

Endpoint deterministico sem IA:

```bash
curl -X POST http://localhost:8000/analisar-nfe-json \
  -F "file=@nota_exemplo.xml"
```

## Contrato da API

### `POST /analisar-nfe`

Recebe multipart form-data:

```text
file=<arquivo.xml>
```

Retorna:

```json
{
  "dados": {
    "cabecalho": {},
    "resumo": {},
    "itens": []
  },
  "relatorio": {
    "titulo": "Analise da NF-e",
    "resumo_executivo": "Texto amigavel.",
    "pontos_atencao": [],
    "itens": [],
    "fontes": [],
    "observacao": "Analise automatizada..."
  }
}
```

`dados` e a saida deterministica do motor fiscal. `relatorio` e texto amigavel gerado pelo Gemini em JSON estruturado.

### `POST /analisar-nfe-json`

Retorna apenas o objeto `dados`, sem chamar Gemini. Use para debug e testes.

## Saida deterministica

Formato resumido:

```json
{
  "cabecalho": {
    "numero": "456",
    "chave_acesso": "3524...",
    "data_emissao": "2024-05-16",
    "natureza_operacao": "Venda",
    "tipo": "saida",
    "uf_emitente": "SP",
    "uf_destinatario": "SP",
    "valor_total": 2074.0
  },
  "resumo": {
    "total_itens": 4,
    "total_alertas": 3,
    "total_divergencias": 3,
    "total_ok": 13,
    "total_revisao_manual": 0
  },
  "itens": [
    {
      "numero_item": 1,
      "produto_nfe": {
        "cProd": "IMP-LASER-01",
        "xProd": "Impressora laser monocromatica",
        "NCM": "84433233",
        "CFOP": "5102"
      },
      "match": {
        "nivel": "NCM_EXATO",
        "score": 100.0,
        "produto_fiscal": {}
      },
      "analises": [
        {
          "tributo": "IPI",
          "status": "divergente",
          "declarado": {},
          "esperado": {},
          "diferenca": {},
          "fonte": {},
          "mensagem": "IPI diverge..."
        }
      ]
    }
  ]
}
```

Status possiveis:

- `ok`: declarado esta dentro da tolerancia.
- `divergente`: aliquota ou valor diverge da regra.
- `sem_regra`: nao ha regra vigente aplicavel.
- `sem_imposto_na_nfe`: tributo nao aparece no item do XML.
- `revisao_manual`: ha ambiguidade ou regra fora do MVP.

## Principios de seguranca

- Nao usar LLM para decidir regra fiscal.
- Nao gerar SQL com LLM.
- Usar queries parametrizadas.
- Nao inventar regra ausente.
- Se nao houver regra aplicavel, retornar `sem_regra`.
- Se houver ambiguidade, retornar `revisao_manual`.

## Limitacoes atuais

- O schema atual nao possui `ean` em `produtos_fiscais`; match nao depende de EAN.
- A busca de regras usa NCM porque o banco pode ter produtos fiscais duplicados por NCM.
- Regra estadual assume SP no MVP.
- Regras diferentes de `ALIQUOTA` e `NAO_TRIBUTADO` tendem a revisao manual.
- A qualidade da analise depende das regras ja carregadas pelo scraper.
