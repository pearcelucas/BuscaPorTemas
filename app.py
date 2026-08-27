"""
Backend da Prognose Jurídica — proxy real (não simulado) para a API Pública
do DataJud (CNJ), pronto para deploy no Vercel (Python runtime / FastAPI).

Implementa exatamente os 3 endpoints descritos na seção 4 do documento
"Arquitetura DataJud" (v2):
  GET /api/tribunais/{alias}/varas
  GET /api/temas
  GET /api/processos?tribunal=&vara=&tema=&pagina=

Cache: em vez de um Redis separado, usa o cache de borda do próprio Vercel
via header Cache-Control (s-maxage). Isso evita bater no DataJud a cada
requisição repetida, sem precisar de mais nenhuma peça de infraestrutura —
suficiente para o volume de uso de um MVP. Se o uso crescer a ponto de
precisar de invalidação manual ou cache compartilhado entre regiões, trocar
por um Redis do Vercel Marketplace (ex.: Upstash) é a evolução natural.

Deploy:
  1. pip install fastapi requests  (localmente, para testar: uvicorn app:app --reload)
  2. vercel deploy  (a partir desta pasta, com a Vercel CLI instalada e logada)
  3. Defina DATAJUD_APIKEY como variável de ambiente no projeto Vercel se quiser
     trocar a chave publicada pelo CNJ sem reeditar o código.
"""

import json
import os
import time
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Configuração — mesma lógica de datajud_client.py, adaptada para servir
# requisições HTTP em vez de rodar como script standalone.
# ---------------------------------------------------------------------------
DATAJUD_APIKEY = os.environ.get(
    "DATAJUD_APIKEY",
    "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",
)
BASE_URL = "https://api-publica.datajud.cnj.jus.br"
HEADERS = {"Authorization": f"APIKey {DATAJUD_APIKEY}", "Content-Type": "application/json"}

TRIBUNAL_ALIASES = {
    # Tribunais de Justiça estaduais — todos os 26 estados + Distrito Federal.
    # Padrão de alias confirmado em produção para tjsp/tjrj/tjmg/tjdft/tjrs;
    # os demais seguem a mesma convenção documentada pelo CNJ (api_publica_<uf>).
    "TJAC": "tjac", "TJAL": "tjal", "TJAP": "tjap", "TJAM": "tjam", "TJBA": "tjba",
    "TJCE": "tjce", "TJDFT": "tjdft", "TJES": "tjes", "TJGO": "tjgo", "TJMA": "tjma",
    "TJMT": "tjmt", "TJMS": "tjms", "TJMG": "tjmg", "TJPA": "tjpa", "TJPB": "tjpb",
    "TJPR": "tjpr", "TJPE": "tjpe", "TJPI": "tjpi", "TJRJ": "tjrj", "TJRN": "tjrn",
    "TJRS": "tjrs", "TJRO": "tjro", "TJRR": "tjrr", "TJSC": "tjsc", "TJSP": "tjsp",
    "TJSE": "tjse", "TJTO": "tjto",
    # Tribunais Regionais do Trabalho — todas as 24 regiões.
    "TRT1": "trt1", "TRT2": "trt2", "TRT3": "trt3", "TRT4": "trt4", "TRT5": "trt5",
    "TRT6": "trt6", "TRT7": "trt7", "TRT8": "trt8", "TRT9": "trt9", "TRT10": "trt10",
    "TRT11": "trt11", "TRT12": "trt12", "TRT13": "trt13", "TRT14": "trt14", "TRT15": "trt15",
    "TRT16": "trt16", "TRT17": "trt17", "TRT18": "trt18", "TRT19": "trt19", "TRT20": "trt20",
    "TRT21": "trt21", "TRT22": "trt22", "TRT23": "trt23", "TRT24": "trt24",
}

CONSULTA_PUBLICA = {
    "TJSP": "https://esaj.tjsp.jus.br/cpopg/open.do",
    "TRT2": "https://pje.trt2.jus.br/consultaprocessual/consulta-cidadao",
}

# Só temas com código de assunto confirmado no CSV oficial do CNJ entram aqui.
# Ver seção 6 do documento de arquitetura para os pendentes.
TEMAS_VERIFICADOS = {
    "acidente_trabalho": {"nome": "Acidente de Trabalho", "assuntos": [14012, 14016, 14048, 14194, 14810]},
    "despejo": {"nome": "Despejo por Inadimplemento", "assuntos": [14915]},
    "repeticao_indebito": {"nome": "Repetição do Indébito", "assuntos": [14925]},
    # "dano_moral": reconfirmado em 27/08 no CSV oficial (assuntos.csv do
    # CNJ, delimitador ";"). "Indenização por Dano Moral" existe como QUATRO
    # nós de agrupamento paralelos na TPU — um por área do direito (Civil
    # geral, Consumidor, e mais dois outros ramos) — cada um com seu próprio
    # conjunto de códigos-filho ativos (cod_filhos_ativos). Nenhuma petição
    # usa o código do nó-pai diretamente, só os filhos. A primeira correção
    # só usou o ramo "Civil geral" (14010) e continuou devolvendo 0 num
    # Juizado Especial Cível — porque causas de juizado são majoritariamente
    # de consumo, ramo diferente (9992). Por isso a lista abaixo junta os
    # filhos ativos dos 4 ramos encontrados, para cobrir o tema
    # independentemente da área do direito do processo:
    #   14010 (Dano Moral / Civil geral,        pai 14007): 15 filhos
    #   9992  (Dano Moral / Consumidor,          pai 9991):  19 filhos
    #   10433 (Dano Moral / outro ramo civil,    pai 10431):  6 filhos
    #   7779  (Dano Moral / outro ramo,          pai 6220):   3 filhos
    "dano_moral": {
        "nome": "Indenização por Dano Moral",
        "assuntos": [
            14016, 14017, 14018, 14019, 14020, 14021, 14022, 14023, 14024,
            14025, 14026, 14027, 14028, 14029, 14030,
            9995, 9996, 10870, 10888, 14162, 14163, 14164, 14165, 14167,
            14168, 14169, 14170, 14171, 14172, 14173, 14174, 14175, 14909,
            14911,
            10434, 10435, 10436, 10437, 14920, 14922,
            6226, 7781, 12042,
        ],
    },
}

app = FastAPI(title="Prognose Jurídica — backend DataJud")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrinja ao domínio real do frontend antes de ir para produção
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def erro_inesperado(request, exc):
    # Temporário, para diagnóstico: expõe o tipo/mensagem da exceção em vez do
    # "Internal Server Error" genérico, para conseguirmos ver a causa real sem
    # precisar acessar os logs da Vercel. Remover/reduzir depois de estabilizado.
    import traceback
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"Erro interno não tratado: {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        },
    )


def _nome_campo(valor):
    """Extrai o campo 'nome' de forma tolerante: em alguns documentos reais do
    DataJud (inconsistência de dados entre tribunais/instâncias) 'classe',
    'assuntos[i]', 'orgaoJulgador' ou 'movimentos[i]' vêm como dict {"nome": ...},
    mas em outros vêm como lista (às vezes até lista aninhada). Sem esse
    tratamento, .get("nome") direto quebra com AttributeError nesses casos."""
    if isinstance(valor, dict):
        return valor.get("nome")
    if isinstance(valor, list):
        for item in valor:
            nome = _nome_campo(item)
            if nome:
                return nome
        return None
    return None


def _post_datajud(endpoint: str, body: dict, tentativas: int = 4) -> dict:
    """POST com backoff exponencial — trata 429/503 sem repassar cru ao cliente."""
    url = f"{BASE_URL}/{endpoint}/_search"
    espera = 1.5
    ultimo_erro = None
    for _ in range(tentativas):
        try:
            resp = requests.post(url, headers=HEADERS, data=json.dumps(body), timeout=20)
        except requests.RequestException as e:
            ultimo_erro = e
            time.sleep(espera)
            espera *= 2
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 503):
            time.sleep(espera)
            espera *= 2
            continue
        raise HTTPException(status_code=502, detail=f"DataJud respondeu {resp.status_code}: {resp.text[:300]}")
    raise HTTPException(status_code=504, detail=f"DataJud indisponível após {tentativas} tentativas ({ultimo_erro})")


@app.get("/api/temas")
def listar_temas(response: Response):
    response.headers["Cache-Control"] = "public, s-maxage=3600"
    return {
        "temas": [
            {"id": key, "nome": val["nome"], "status": "verificado"}
            for key, val in TEMAS_VERIFICADOS.items()
        ]
    }


@app.get("/api/tribunais/{alias}/varas")
def listar_varas(alias: str, response: Response, tamanho: int = 200):
    alias = alias.upper()
    if alias not in TRIBUNAL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Tribunal '{alias}' não configurado.")
    endpoint = f"api_publica_{TRIBUNAL_ALIASES[alias]}"
    body = {"size": 0, "aggs": {"varas": {"terms": {"field": "orgaoJulgador.nome.keyword", "size": tamanho}}}}
    data = _post_datajud(endpoint, body)
    buckets = data.get("aggregations", {}).get("varas", {}).get("buckets", [])
    response.headers["Cache-Control"] = "public, s-maxage=86400, stale-while-revalidate=3600"
    return {
        "tribunal": alias,
        "varas": [{"nome": b["key"], "processosIndexados": b["doc_count"]} for b in buckets],
    }


@app.get("/api/processos")
def buscar_processos(
    response: Response,
    tribunal: str = Query(..., description="Alias do tribunal, ex.: TJSP"),
    vara: str = Query(..., description="Nome exato da vara, vindo de /api/tribunais/{alias}/varas"),
    tema: str = Query(..., description="Chave do tema, vinda de /api/temas"),
    pagina: int = Query(1, ge=1),
    tamanho: int = Query(20, ge=1, le=100),
):
    tribunal = tribunal.upper()
    if tribunal not in TRIBUNAL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Tribunal '{tribunal}' não configurado.")
    if tema not in TEMAS_VERIFICADOS:
        raise HTTPException(
            status_code=422,
            detail=f"Tema '{tema}' ainda não tem código de assunto confirmado — veja a seção 6 da arquitetura.",
        )

    endpoint = f"api_publica_{TRIBUNAL_ALIASES[tribunal]}"
    assuntos = TEMAS_VERIFICADOS[tema]["assuntos"]

    query = {
        "size": tamanho,
        "from": (pagina - 1) * tamanho,
        "query": {"bool": {"must": [
            # term (não match) no subcampo .keyword: precisa ser IGUAL ao nome
            # exato da vara devolvido por /api/tribunais/{alias}/varas. Um
            # "match" de texto livre aqui casava com qualquer vara que
            # compartilhasse palavras comuns ("vara", "juizado", "cível"...),
            # trazendo processos de comarcas erradas.
            {"term": {"orgaoJulgador.nome.keyword": vara}},
            {"terms": {"assuntos.codigo": assuntos}},
        ]}},
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    data = _post_datajud(endpoint, query)
    hits = data.get("hits", {}).get("hits", [])

    processos = []
    for h in hits:
        src = h["_source"]
        movimentos = src.get("movimentos") or []
        processos.append({
            "numeroProcesso": src.get("numeroProcesso"),
            "classe": _nome_campo(src.get("classe")),
            "assuntos": [n for n in (_nome_campo(a) for a in (src.get("assuntos") or [])) if n],
            "orgaoJulgador": _nome_campo(src.get("orgaoJulgador")),
            "grau": src.get("grau"),
            "dataAjuizamento": src.get("dataAjuizamento"),
            "ultimoMovimento": _nome_campo(movimentos[-1]) if movimentos else None,
        })

    response.headers["Cache-Control"] = "public, s-maxage=21600, stale-while-revalidate=3600"
    return {
        "tribunal": tribunal,
        "vara": vara,
        "tema": TEMAS_VERIFICADOS[tema]["nome"],
        "pagina": pagina,
        "total_nesta_pagina": len(processos),
        "consultaPublica": CONSULTA_PUBLICA.get(
            tribunal,
            f"Busque \"consulta processual {tribunal}\" no site oficial do tribunal — URL ainda não confirmada.",
        ),
        "processos": processos,
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}
