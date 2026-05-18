import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ai_report import gerar_resposta_amigavel
from matcher import processar


load_dotenv()

app = FastAPI(
    title="Tricomplex Extractor API",
    description="Analise de XML de NF-e contra regras tributarias e relatorio amigavel em JSON.",
    version="0.1.0",
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _validar_xml_upload(file: UploadFile):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix and suffix != ".xml":
        raise HTTPException(status_code=400, detail="Envie um arquivo XML de NF-e.")


async def _salvar_upload_temporario(file: UploadFile):
    _validar_xml_upload(file)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Arquivo XML vazio.")

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".xml")
    try:
        temp.write(content)
        temp.flush()
        return temp.name
    finally:
        temp.close()


def _remover_temporario(path):
    try:
        os.unlink(path)
    except OSError:
        pass


@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini_configurado": bool(os.getenv("GEMINI_API_KEY")),
        "modelo": os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
    }


@app.post("/analisar-nfe")
async def analisar_nfe(file: UploadFile = File(...)):
    temp_path = await _salvar_upload_temporario(file)
    try:
        resultado = processar(temp_path)
        relatorio = gerar_resposta_amigavel(resultado)
        return JSONResponse(jsonable_encoder({
            "dados": resultado,
            "relatorio": relatorio,
        }))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        _remover_temporario(temp_path)


@app.post("/analisar-nfe-json")
async def analisar_nfe_json(file: UploadFile = File(...)):
    temp_path = await _salvar_upload_temporario(file)
    try:
        resultado = processar(temp_path)
        return JSONResponse(jsonable_encoder(resultado))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        _remover_temporario(temp_path)
