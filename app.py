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
from fastapi.responses import HTMLResponse

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

# ---------------------------------------------------------------------------
# Front-end (index.html) embutido como string — servido na raiz do mesmo
# domínio do backend (rota "/" abaixo), para não depender de um segundo
# projeto/URL na Vercel nem de bundling de arquivo estático (o runtime
# Python da Vercel, no modo zero-config usado aqui com app.py na raiz do
# projeto, roteia tudo para esta aplicação — um index.html solto ao lado
# não seria servido como estático). Para atualizar o front-end, gere este
# bloco de novo a partir do index.html mais recente.
FRONTEND_HTML = r"""<title>Prognose Jurídica</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">

<style>
  /* ---------- Tokens ---------- */
  :root{
    --paper:#F1EFE9;
    --surface:#FFFFFF;
    --surface-2:#EAE7DE;
    --ink:#1B1F27;
    --ink-muted:#5B5F6B;
    --ink-faint:#8B8E97;
    --border:#DEDACD;
    --border-strong:#C9C4B2;

    --navy:#1F2A44;
    --navy-ink:#F1EFE9;
    --accent:#8A6A2F;
    --accent-ink:#5B4720;
    --accent-soft:#EFE4CC;

    --good:#1E7D4B;
    --good-soft:#E1EFE6;
    --warn:#A8721C;
    --warn-soft:#F3E7D2;
    --critical:#B3261E;
    --critical-soft:#F6E1DF;

    --shadow: 0 1px 2px rgba(27,31,39,0.06), 0 6px 20px -8px rgba(27,31,39,0.16);
    --radius: 10px;
    --font-display:'Fraunces', Georgia, 'Times New Roman', serif;
    --font-body:'IBM Plex Sans', -apple-system, 'Segoe UI', sans-serif;
    --font-mono:'IBM Plex Mono', 'SFMono-Regular', Consolas, monospace;
  }

  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --paper:#12151C;
      --surface:#191D26;
      --surface-2:#20242E;
      --ink:#EBE9E2;
      --ink-muted:#A3A7B2;
      --ink-faint:#6D717C;
      --border:#2B303C;
      --border-strong:#3A3F4C;

      --navy:#2B3A60;
      --navy-ink:#EDEBE3;
      --accent:#D2AE6E;
      --accent-ink:#F1E3C4;
      --accent-soft:#332A18;

      --good:#4FB57F;
      --good-soft:#173226;
      --warn:#D9A247;
      --warn-soft:#332711;
      --critical:#E28079;
      --critical-soft:#391E1C;

      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 10px 30px -10px rgba(0,0,0,0.5);
    }
  }
  :root[data-theme="dark"]{
    --paper:#12151C;
    --surface:#191D26;
    --surface-2:#20242E;
    --ink:#EBE9E2;
    --ink-muted:#A3A7B2;
    --ink-faint:#6D717C;
    --border:#2B303C;
    --border-strong:#3A3F4C;

    --navy:#2B3A60;
    --navy-ink:#EDEBE3;
    --accent:#D2AE6E;
    --accent-ink:#F1E3C4;
    --accent-soft:#332A18;

    --good:#4FB57F;
    --good-soft:#173226;
    --warn:#D9A247;
    --warn-soft:#332711;
    --critical:#E28079;
    --critical-soft:#391E1C;

    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 10px 30px -10px rgba(0,0,0,0.5);
  }

  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;}
  body{
    background:var(--paper);
    color:var(--ink);
    font-family:var(--font-body);
    font-size:15px;
    line-height:1.5;
    -webkit-font-smoothing:antialiased;
  }
  @media (prefers-reduced-motion: reduce){
    *{animation-duration:0.01ms !important; transition-duration:0.01ms !important;}
  }

  a{color:var(--accent-ink);}
  :root[data-theme="dark"] a, @media (prefers-color-scheme: dark){ a{color:var(--accent);} }

  ::selection{ background:var(--accent-soft); color:var(--ink); }

  button, input, select, textarea{ font-family:inherit; font-size:inherit; color:inherit; }
  :focus-visible{ outline:2px solid var(--accent); outline-offset:2px; }

  .app{ max-width:1080px; margin:0 auto; padding:0 24px 80px; }

  /* ---------- Header ---------- */
  header.top{
    border-bottom:1px solid var(--border);
    background:var(--paper);
    position:sticky; top:0; z-index:20;
    backdrop-filter: blur(6px);
  }
  header.top .inner{
    max-width:1080px; margin:0 auto; padding:18px 24px;
    display:flex; align-items:center; justify-content:space-between; gap:16px;
  }
  .brand{ display:flex; align-items:baseline; gap:10px; }
  .brand .mark{
    font-family:var(--font-display); font-weight:600; font-size:22px; letter-spacing:0.2px;
    color:var(--ink);
  }
  .brand .mark em{ font-style:italic; color:var(--accent-ink); }
  :root[data-theme="dark"] .brand .mark em, @media (prefers-color-scheme:dark){ .brand .mark em{ color:var(--accent);} }
  .brand .tag{ font-size:12.5px; color:var(--ink-faint); font-family:var(--font-body); }

  .info-btn{
    display:inline-flex; align-items:center; gap:8px;
    background:transparent; border:1px solid var(--border-strong);
    color:var(--ink-muted); padding:8px 14px; border-radius:999px;
    cursor:pointer; font-size:13px; font-weight:500;
    transition:border-color .15s ease, color .15s ease;
  }
  .info-btn:hover{ border-color:var(--accent); color:var(--ink); }

  /* ---------- Stepper ---------- */
  .stepper{
    display:flex; align-items:center; gap:0; margin:28px 0 34px;
    overflow-x:auto;
  }
  .step-node{ display:flex; align-items:center; gap:10px; flex-shrink:0; }
  .step-circle{
    width:28px; height:28px; border-radius:50%;
    display:flex; align-items:center; justify-content:center;
    font-family:var(--font-mono); font-size:12px; font-weight:600;
    border:1.5px solid var(--border-strong); color:var(--ink-faint);
    background:var(--surface);
    transition:all .2s ease;
  }
  .step-label{ font-size:13px; color:var(--ink-faint); font-weight:500; white-space:nowrap; }
  .step-node.active .step-circle{ border-color:var(--navy); background:var(--navy); color:var(--navy-ink); }
  .step-node.active .step-label{ color:var(--ink); }
  .step-node.done .step-circle{ border-color:var(--good); background:var(--good-soft); color:var(--good); }
  .step-node.done .step-label{ color:var(--ink-muted); }
  .step-connector{ width:32px; height:1px; background:var(--border-strong); margin:0 10px; flex-shrink:0; }
  .step-node.clickable{ cursor:pointer; }

  /* ---------- Panels ---------- */
  .panel{ display:none; }
  .panel.active{ display:block; animation:fade .35s ease; }
  @keyframes fade{ from{opacity:0; transform:translateY(4px);} to{opacity:1; transform:translateY(0);} }

  .panel-head{ margin-bottom:22px; }
  .eyebrow{
    font-family:var(--font-mono); font-size:11.5px; letter-spacing:0.08em; text-transform:uppercase;
    color:var(--accent-ink); font-weight:600; margin:0 0 6px;
  }
  :root[data-theme="dark"] .eyebrow, @media (prefers-color-scheme:dark){ .eyebrow{ color:var(--accent);} }
  .panel-head h1{
    font-family:var(--font-display); font-weight:600; font-size:30px; margin:0 0 8px;
    text-wrap:balance; color:var(--ink);
  }
  .panel-head p{ margin:0; color:var(--ink-muted); font-size:14.5px; max-width:62ch; }

  .breadcrumb{
    display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:18px;
    font-size:13px; color:var(--ink-muted);
  }
  .breadcrumb .chip{
    background:var(--surface-2); border:1px solid var(--border); border-radius:999px;
    padding:5px 12px; color:var(--ink); font-weight:500;
  }
  .breadcrumb .sep{ color:var(--ink-faint); }

  /* ---------- Search ---------- */
  .search-row{ margin-bottom:20px; }
  .search-input{
    width:100%; padding:12px 16px; border-radius:var(--radius);
    border:1px solid var(--border-strong); background:var(--surface); color:var(--ink);
  }
  .search-input::placeholder{ color:var(--ink-faint); }

  /* ---------- Cards grid ---------- */
  .grid{ display:grid; grid-template-columns:repeat(auto-fill, minmax(240px,1fr)); gap:14px; }

  .card{
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    padding:18px; cursor:pointer; text-align:left; width:100%;
    box-shadow:var(--shadow);
    transition:border-color .15s ease, transform .15s ease;
    display:flex; flex-direction:column; gap:10px;
  }
  .card:hover{ border-color:var(--accent); transform:translateY(-2px); }
  .card .card-title{ font-family:var(--font-display); font-weight:600; font-size:17px; color:var(--ink); text-wrap:balance; }
  .card .card-meta{ font-size:12.5px; color:var(--ink-faint); display:flex; flex-wrap:wrap; gap:6px 10px; }
  .type-badge{
    align-self:flex-start; font-size:10.5px; font-weight:600; letter-spacing:0.04em; text-transform:uppercase;
    padding:3px 9px; border-radius:5px; background:var(--surface-2); color:var(--ink-muted);
  }

  /* ---------- Buttons ---------- */
  .btn{
    display:inline-flex; align-items:center; gap:8px; justify-content:center;
    padding:12px 22px; border-radius:8px; border:1px solid transparent;
    font-weight:600; font-size:14px; cursor:pointer; transition:filter .15s ease, background .15s ease;
  }
  .btn-primary{ background:var(--navy); color:var(--navy-ink); }
  .btn-primary:hover{ filter:brightness(1.12); }
  .btn-primary:disabled{ opacity:0.45; cursor:not-allowed; filter:none; }
  .btn-ghost{ background:transparent; border-color:var(--border-strong); color:var(--ink-muted); }
  .btn-ghost:hover{ border-color:var(--accent); color:var(--ink); }
  .btn-ghost:disabled{ opacity:0.45; cursor:not-allowed; }
  .mini-btn{ padding:7px 14px; font-size:12.5px; border-radius:6px; }

  /* ---------- Step: form ---------- */
  .form-card{
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    padding:24px; box-shadow:var(--shadow);
  }
  label.field-label{ display:block; font-size:13px; font-weight:600; color:var(--ink); margin-bottom:8px; }
  .hint{ font-size:12.5px; color:var(--ink-faint); margin-top:6px; }
  textarea.inicial{
    width:100%; min-height:180px; resize:vertical; padding:14px 16px; border-radius:8px;
    border:1px solid var(--border-strong); background:var(--paper); color:var(--ink);
    font-family:var(--font-mono); font-size:13px; line-height:1.6;
  }
  .divider-or{
    display:flex; align-items:center; gap:12px; margin:20px 0; color:var(--ink-faint); font-size:12px;
    text-transform:uppercase; letter-spacing:.06em; font-weight:600;
  }
  .divider-or::before, .divider-or::after{ content:""; flex:1; height:1px; background:var(--border); }
  select.tema-select{
    width:100%; padding:11px 14px; border-radius:8px; border:1px solid var(--border-strong);
    background:var(--surface); color:var(--ink);
  }
  .form-actions{ margin-top:22px; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }

  .tema-chip{
    display:inline-flex; align-items:center; gap:8px; background:var(--accent-soft); color:var(--accent-ink);
    border-radius:999px; padding:7px 14px; font-size:13px; font-weight:600; margin-top:14px;
  }
  :root[data-theme="dark"] .tema-chip, @media (prefers-color-scheme:dark){ .tema-chip{ color:var(--accent);} }

  /* ---------- Step final: resultados reais ---------- */
  .result-header{
    display:flex; justify-content:space-between; align-items:flex-start; gap:20px; flex-wrap:wrap;
    margin-bottom:18px;
  }
  .result-meta h2{ font-family:var(--font-display); font-size:22px; font-weight:600; margin:0 0 4px; text-wrap:balance; }
  .result-meta .sub{ color:var(--ink-muted); font-size:13.5px; }

  .table-card{
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    box-shadow:var(--shadow); overflow:hidden; margin-bottom:16px;
  }
  .table-card h3{ font-family:var(--font-display); font-size:16px; font-weight:600; margin:0; padding:16px 20px; border-bottom:1px solid var(--border); }
  .table-scroll{ overflow-x:auto; }
  table.precedentes{ width:100%; border-collapse:collapse; font-size:13px; min-width:720px; }
  table.precedentes th{
    text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--ink-faint);
    padding:10px 20px; border-bottom:1px solid var(--border); font-weight:600;
  }
  table.precedentes td{ padding:11px 20px; border-bottom:1px solid var(--border); color:var(--ink); vertical-align:top; }
  table.precedentes tr:last-child td{ border-bottom:none; }
  table.precedentes td.num{ font-family:var(--font-mono); font-size:12px; color:var(--ink-muted); white-space:nowrap; }

  .badge{
    display:inline-flex; align-items:center; gap:5px; font-size:11.5px; font-weight:600;
    padding:4px 10px; border-radius:999px;
  }
  .badge.good{ background:var(--good-soft); color:var(--good); }
  .badge.warn{ background:var(--warn-soft); color:var(--warn); }
  .badge.critical{ background:var(--critical-soft); color:var(--critical); }
  .badge .dot{ background:currentColor; width:8px; height:8px; border-radius:50%; display:inline-block; }

  .result-disclaimer{
    display:flex; gap:12px; background:var(--surface-2); border:1px solid var(--border);
    border-radius:var(--radius); padding:16px 18px; font-size:12.5px; color:var(--ink-muted); align-items:flex-start;
  }
  .result-disclaimer .icon{ font-size:16px; flex-shrink:0; margin-top:1px; }

  /* ---------- Footer ---------- */
  footer.legal-strip{
    border-top:1px solid var(--border); margin-top:60px; padding:18px 0 40px;
  }
  footer.legal-strip .inner{
    max-width:1080px; margin:0 auto; padding:0 24px;
    display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap;
    font-size:12px; color:var(--ink-faint);
  }
  footer.legal-strip a{ color:var(--ink-muted); text-decoration:underline; cursor:pointer; }

  /* ---------- Modal ---------- */
  .modal-backdrop{
    display:none; position:fixed; inset:0; background:rgba(10,12,16,0.5); z-index:100;
    align-items:flex-start; justify-content:center; padding:6vh 20px; overflow-y:auto;
  }
  .modal-backdrop.open{ display:flex; }
  .modal{
    background:var(--surface); border:1px solid var(--border); border-radius:14px; max-width:640px; width:100%;
    padding:30px 30px 26px; box-shadow:var(--shadow);
  }
  .modal h2{ font-family:var(--font-display); font-size:22px; margin:0 0 4px; }
  .modal .modal-sub{ color:var(--ink-faint); font-size:12.5px; margin-bottom:20px; }
  .modal h3{ font-size:13.5px; margin:20px 0 8px; color:var(--ink); }
  .modal h3:first-of-type{ margin-top:0; }
  .modal p{ font-size:13.5px; color:var(--ink-muted); margin:0 0 4px; }
  .modal ul{ margin:6px 0 0; padding-left:18px; font-size:13.5px; color:var(--ink-muted); }
  .modal li{ margin-bottom:5px; }
  .modal code{ font-family:var(--font-mono); font-size:12px; background:var(--surface-2); padding:1px 5px; border-radius:4px; }
  .modal-close{
    float:right; background:none; border:none; color:var(--ink-faint); font-size:20px; cursor:pointer; line-height:1;
  }
</style>

<div class="app">
  <header class="top">
    <div class="inner">
      <div class="brand">
        <span class="mark">Prog<em>nose</em> Jurídica</span>
        <span class="tag">consulta de processos reais por estado, tribunal e vara · dados ao vivo do DataJud (CNJ)</span>
      </div>
      <button class="info-btn" id="openInfo" type="button">ℹ️ Avisos legais &amp; fontes de dados</button>
    </div>
  </header>

  <main>
    <nav class="stepper" id="stepper" aria-label="Etapas da consulta"></nav>

    <!-- STEP 1: ESTADO -->
    <section class="panel active" data-panel="1">
      <div class="panel-head">
        <p class="eyebrow">Etapa 1 de 5</p>
        <h1>Selecione o estado</h1>
        <p>Todos os 26 estados + Distrito Federal estão disponíveis. Ao escolher um estado, mostramos o Tribunal de Justiça estadual e o(s) Tribunal(is) Regional(is) do Trabalho que atendem essa unidade federativa, com dados reais e ao vivo do DataJud.</p>
      </div>
      <div class="search-row">
        <input class="search-input" id="estadoSearch" type="text" placeholder="Buscar estado por nome, UF ou região…">
      </div>
      <div class="grid" id="estadoGrid"></div>
    </section>

    <!-- STEP 2: TRIBUNAL -->
    <section class="panel" data-panel="2">
      <div class="panel-head">
        <p class="eyebrow">Etapa 2 de 5</p>
        <h1>Selecione o tribunal</h1>
        <p>Dados reais (API pública do DataJud/CNJ). Nesta fase cobrimos Justiça Estadual e Justiça do Trabalho — Justiça Federal, Eleitoral e tribunais superiores ainda não estão mapeados.</p>
      </div>
      <div class="breadcrumb" id="breadcrumbTribunal"></div>
      <div class="grid" id="tribunalGrid"></div>
      <div class="form-actions" style="margin-top:20px;">
        <button class="btn btn-ghost" data-goto="1">← Trocar estado</button>
      </div>
    </section>

    <!-- STEP 3: VARA -->
    <section class="panel" data-panel="3">
      <div class="panel-head">
        <p class="eyebrow">Etapa 3 de 5</p>
        <h1>Selecione a vara / órgão julgador</h1>
        <p>Lista real, obtida ao vivo do DataJud, ordenada pelo volume de processos indexados — já reflete a comarca (ex.: "FAZENDA PÚBLICA DE CAMPINAS"). O DataJud identifica a vara/órgão julgador, não a pessoa do magistrado — veja "Avisos legais" para o motivo.</p>
      </div>
      <div class="breadcrumb" id="breadcrumbVara"></div>
      <div class="search-row">
        <input class="search-input" id="varaSearch" type="text" placeholder="Buscar vara/órgão julgador ou comarca por nome…">
      </div>
      <div id="varaStatus" class="hint" style="margin-bottom:14px;"></div>
      <div class="grid" id="varaGrid"></div>
      <div class="form-actions" style="margin-top:20px;">
        <button class="btn btn-ghost" data-goto="2">← Trocar tribunal</button>
      </div>
    </section>

    <!-- STEP 4: TEMA -->
    <section class="panel" data-panel="4">
      <div class="panel-head">
        <p class="eyebrow">Etapa 4 de 5</p>
        <h1>Selecione o tema</h1>
        <p>Só aparecem aqui os temas com código de assunto já confirmado na Tabela Processual Unificada do CNJ — os demais ainda estão sendo mapeados.</p>
      </div>
      <div class="breadcrumb" id="breadcrumbTema"></div>

      <div class="form-card">
        <label class="field-label" for="inicialText">Texto da petição inicial (opcional)</label>
        <textarea class="inicial" id="inicialText" placeholder="Cole aqui o texto da inicial — o sistema tenta identificar automaticamente um dos temas já mapeados…"></textarea>
        <p class="hint">Detecção automática cobre só os temas já mapeados na lista abaixo. Fora deles, selecione manualmente.</p>

        <div class="divider-or">ou selecione manualmente</div>

        <label class="field-label" for="temaSelect">Tema / matéria da ação</label>
        <select class="tema-select" id="temaSelect"><option value="">Carregando temas…</option></select>

        <div id="temaDetected"></div>

        <div class="form-actions">
          <button class="btn btn-primary" id="analyzeBtn" type="button">Buscar processos reais →</button>
          <button class="btn btn-ghost" data-goto="3">← Trocar vara</button>
        </div>
      </div>
    </section>

    <!-- STEP 5: RESULTADO (processos reais) -->
    <section class="panel" data-panel="5">
      <div class="breadcrumb" id="breadcrumbResultado"></div>
      <div class="result-header">
        <div class="result-meta">
          <h2 id="resultTitle">Processos localizados</h2>
          <div class="sub" id="resultSub"></div>
        </div>
        <button class="btn btn-ghost mini-btn" id="resetBtn" type="button">↺ Nova consulta</button>
      </div>

      <div id="resultStatus" class="hint" style="margin-bottom:16px;"></div>

      <div class="table-card">
        <h3>Processos reais encontrados</h3>
        <div class="table-scroll">
          <table class="precedentes">
            <thead>
              <tr><th>Nº do processo</th><th>Classe</th><th>Órgão julgador</th><th>Ajuizamento</th><th>Último movimento</th></tr>
            </thead>
            <tbody id="precedentesBody"></tbody>
          </table>
        </div>
      </div>

      <div class="form-actions" style="margin-bottom:18px;">
        <button class="btn btn-ghost mini-btn" id="carregarMaisBtn" type="button" style="display:none;">Carregar mais processos</button>
      </div>

      <div class="result-disclaimer">
        <span class="icon">⚖️</span>
        <span>Lista real de processos, obtida ao vivo da API pública do DataJud (CNJ) — não é uma amostra fictícia. O DataJud não calcula probabilidade de êxito nem valor de condenação; para ver andamento, partes e resultado de cada processo, consulte-o individualmente no portal público do tribunal, usando o número acima. <b id="consultaPublicaLink"></b></span>
      </div>
    </section>
  </main>

  <footer class="legal-strip">
    <div class="inner">
      <span>Fase com dados reais (DataJud/CNJ) — nenhuma probabilidade ou valor é estimado; cada processo listado deve ser consultado individualmente por você no portal do tribunal correspondente.</span>
      <a id="openInfo2">Avisos legais, LGPD e fontes de dados →</a>
    </div>
  </footer>
</div>

<div class="modal-backdrop" id="infoModal">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="infoTitle">
    <button class="modal-close" id="closeInfo" aria-label="Fechar">×</button>
    <h2 id="infoTitle">Avisos legais e fontes de dados</h2>
    <p class="modal-sub">Leia antes de usar os resultados desta ferramenta em qualquer decisão real.</p>

    <h3>1. O que esta ferramenta é (e não é)</h3>
    <p>Os estados, tribunais, varas, temas e processos exibidos aqui são <b>dados reais</b>, obtidos ao vivo da API pública do DataJud (CNJ). Esta ferramenta <b>não calcula</b> probabilidade de êxito nem valor de condenação — ela localiza processos reais pelo tema e pela vara, para que você mesmo(a) os consulte individualmente no portal público do tribunal correspondente.</p>
    <p>A navegação vai até <b>Estado → Tribunal → Vara → Tema → Processos</b> — não até um magistrado individual. É uma decisão deliberada, não uma limitação de interface: a fonte de dados (API pública do DataJud) identifica a vara/órgão julgador, não a pessoa do juiz. O nome do magistrado, quando exposto, aparece no "órgão julgador" de cada processo (ex.: "Juízo Titular I") ou no portal de consulta pública do tribunal.</p>

    <h3>2. Cobertura atual</h3>
    <ul>
      <li><b>Todos os 26 estados + Distrito Federal</b> estão disponíveis na Etapa 1. Para cada um, mostramos o Tribunal de Justiça estadual e o(s) Tribunal(is) Regional(is) do Trabalho correspondentes (alguns estados dividem a Justiça do Trabalho entre duas regiões — é o caso de São Paulo, coberto por TRT-2 e TRT-15).</li>
      <li><b>Ramos ainda não mapeados:</b> Justiça Federal (TRFs), Justiça Eleitoral (TREs) e tribunais superiores (STJ, STF, TST, TSE) não fazem parte desta fase.</li>
      <li><b>Temas mapeados nesta fase:</b> apenas os que já têm código de assunto confirmado na Tabela Processual Unificada do CNJ (exibidos na Etapa 4). Outros temas exigem confirmar o código antes de entrar na lista — para evitar mostrar resultado errado por causa de um código chutado.</li>
      <li><b>Confirmação dos aliases de tribunal:</b> a convenção de nomes usada pelo DataJud (<code>api_publica_&lt;alias&gt;</code>) foi verificada em produção para TJSP, TJRJ, TJMG, TJDFT, TJRS e TRT-2; os demais tribunais seguem o mesmo padrão documentado pelo CNJ, mas ainda não foram testados um a um — se algum não responder, o erro aparece na tela em vez de travar a busca.</li>
      <li><b>O que o DataJud NÃO traz</b> — e isso muda o desenho do produto: não há inteiro teor das decisões, não há valor de condenação estruturado e não há, na maioria dos registros, o nome pessoal do magistrado. Os dados também têm defasagem (às vezes dias/semanas) e limites de requisição restritivos em uso intenso.</li>
      <li><b>Para o andamento completo, as partes, o valor e o resultado de cada processo:</b> use o número do processo listado e consulte no sistema público do tribunal (cada um tem o seu: e-SAJ, PJe, Projudi…). A URL de consulta pública só está confirmada para TJSP e TRT-2 nesta fase — para os demais tribunais, a tela indica que a URL ainda não foi conferida.</li>
    </ul>

    <h3>3. LGPD</h3>
    <p>Decisões e andamentos judiciais são, em regra, públicos — mas envolvem dados pessoais das partes (e, por vezes, dados sensíveis: saúde, filiação, condição trabalhista). Ao reaproveitar esses dados internamente, observe base legal adequada para o tratamento (execução de obrigação legal ou legítimo interesse, conforme o caso) e uma política de retenção e descarte.</p>

    <h3>4. Publicidade da advocacia (OAB)</h3>
    <p>O Provimento nº 205/2021 do Conselho Federal da OAB veda a captação de clientela por meio de promessa de resultado. Como esta ferramenta não estima probabilidade nem valor, ela deve continuar sendo usada como <b>apoio interno de pesquisa</b> — nunca apresentada a clientes ou terceiros como previsão de resultado.</p>

    <h3>5. Dados reais, mas não é aconselhamento definitivo</h3>
    <p>Os processos listados são reais, mas a leitura do que eles significam para o seu caso depende da análise humana de cada um — fatos, provas e fundamentos concretos de cada autos. Confira sempre a fonte oficial antes de usar qualquer informação em uma peça ou estratégia.</p>
  </div>
</div>

<script>
(function(){
  "use strict";

  /* =====================================================================
     ATENÇÃO: se o domínio do backend na Vercel mudar (ex.: recriar o
     projeto), atualize esta constante com a nova URL.
     ===================================================================== */
  const API_BASE = "https://busca-por-temas-w186.vercel.app";

  /* ---------- Estados (26 + DF) ---------- */
  const ESTADOS = [
    {uf:"AC", nome:"Acre", capital:"Rio Branco", regiao:"Norte"},
    {uf:"AL", nome:"Alagoas", capital:"Maceió", regiao:"Nordeste"},
    {uf:"AP", nome:"Amapá", capital:"Macapá", regiao:"Norte"},
    {uf:"AM", nome:"Amazonas", capital:"Manaus", regiao:"Norte"},
    {uf:"BA", nome:"Bahia", capital:"Salvador", regiao:"Nordeste"},
    {uf:"CE", nome:"Ceará", capital:"Fortaleza", regiao:"Nordeste"},
    {uf:"DF", nome:"Distrito Federal", capital:"Brasília", regiao:"Centro-Oeste"},
    {uf:"ES", nome:"Espírito Santo", capital:"Vitória", regiao:"Sudeste"},
    {uf:"GO", nome:"Goiás", capital:"Goiânia", regiao:"Centro-Oeste"},
    {uf:"MA", nome:"Maranhão", capital:"São Luís", regiao:"Nordeste"},
    {uf:"MT", nome:"Mato Grosso", capital:"Cuiabá", regiao:"Centro-Oeste"},
    {uf:"MS", nome:"Mato Grosso do Sul", capital:"Campo Grande", regiao:"Centro-Oeste"},
    {uf:"MG", nome:"Minas Gerais", capital:"Belo Horizonte", regiao:"Sudeste"},
    {uf:"PA", nome:"Pará", capital:"Belém", regiao:"Norte"},
    {uf:"PB", nome:"Paraíba", capital:"João Pessoa", regiao:"Nordeste"},
    {uf:"PR", nome:"Paraná", capital:"Curitiba", regiao:"Sul"},
    {uf:"PE", nome:"Pernambuco", capital:"Recife", regiao:"Nordeste"},
    {uf:"PI", nome:"Piauí", capital:"Teresina", regiao:"Nordeste"},
    {uf:"RJ", nome:"Rio de Janeiro", capital:"Rio de Janeiro", regiao:"Sudeste"},
    {uf:"RN", nome:"Rio Grande do Norte", capital:"Natal", regiao:"Nordeste"},
    {uf:"RS", nome:"Rio Grande do Sul", capital:"Porto Alegre", regiao:"Sul"},
    {uf:"RO", nome:"Rondônia", capital:"Porto Velho", regiao:"Norte"},
    {uf:"RR", nome:"Roraima", capital:"Boa Vista", regiao:"Norte"},
    {uf:"SC", nome:"Santa Catarina", capital:"Florianópolis", regiao:"Sul"},
    {uf:"SP", nome:"São Paulo", capital:"São Paulo", regiao:"Sudeste"},
    {uf:"SE", nome:"Sergipe", capital:"Aracaju", regiao:"Nordeste"},
    {uf:"TO", nome:"Tocantins", capital:"Palmas", regiao:"Norte"},
  ];

  /* ---------- Tribunal Regional do Trabalho por UF ----------
     Alguns TRTs cobrem mais de um estado (ex.: TRT-8 = PA+AP); São Paulo é
     o único estado dividido em duas regiões próprias (TRT-2 e TRT-15). */
  const TRT_POR_UF = {
    AC:[14], AL:[19], AP:[8], AM:[11], BA:[5], CE:[7], DF:[10], ES:[17], GO:[18],
    MA:[16], MT:[23], MS:[24], MG:[3], PA:[8], PB:[13], PR:[9], PE:[6], PI:[22],
    RJ:[1], RN:[21], RS:[4], RO:[14], RR:[11], SC:[12], SP:[2,15], SE:[20], TO:[10],
  };

  function tribunaisDoEstado(estado){
    const lista = [];
    if(estado.uf === "DF"){
      lista.push({alias:"TJDFT", nome:"TJDFT", desc:"Tribunal de Justiça do Distrito Federal e Territórios", tipo:"Tribunal de Justiça"});
    } else {
      lista.push({alias:"TJ"+estado.uf, nome:"TJ"+estado.uf, desc:"Tribunal de Justiça de "+estado.nome, tipo:"Tribunal de Justiça"});
    }
    (TRT_POR_UF[estado.uf] || []).forEach(function(n){
      lista.push({alias:"TRT"+n, nome:"TRT-"+n, desc:"Tribunal Regional do Trabalho da "+n+"ª Região", tipo:"Justiça do Trabalho"});
    });
    return lista;
  }

  /* ---------- Palavras-chave para detecção automática de tema ----------
     Cobre só os temas que o backend já confirma (ver /api/temas). */
  const KW_POR_TEMA = {
    dano_moral: ["dano moral","danos morais","ofensa","constrangimento","abalo psíquico","abalo psiquico"],
    acidente_trabalho: ["acidente de trabalho","acidente em serviço","acidente em servico","incapacidade laboral","auxílio-doença","auxilio-doenca","cat "],
    despejo: ["despejo","locação","locacao","falta de pagamento","aluguel"],
    repeticao_indebito: ["cobrança indevida","cobranca indevida","repetição de indébito","repeticao de indebito","débito indevido","debito indevido"],
  };

  /* ---------- Fetch helper ---------- */
  function fetchJSON(url){
    return fetch(url).then(function(resp){
      return resp.json().catch(function(){ return null; }).then(function(body){
        if(!resp.ok){
          const msg = (body && body.detail) ? body.detail : ("Erro HTTP " + resp.status);
          throw new Error(msg);
        }
        return body;
      });
    });
  }

  /* ---------- Formatação ---------- */
  function fmtDataProcesso(v){
    if(!v) return "—";
    const s = String(v);
    if(/^\d{4}-\d{2}-\d{2}T/.test(s)){
      const d = new Date(s);
      if(!isNaN(d)) return d.toLocaleDateString("pt-BR");
    }
    const m = s.match(/^(\d{4})(\d{2})(\d{2})/);
    if(m) return m[3]+"/"+m[2]+"/"+m[1];
    return s;
  }

  /* ---------- Estado da aplicação ---------- */
  const state = { estado:null, tribunal:null, vara:null, tema:null, pagina:1, processosAcumulados:[] };
  let varasCache = null;
  let temasCache = null;

  const STEPS = [
    {n:1, label:"Estado"},
    {n:2, label:"Tribunal"},
    {n:3, label:"Vara"},
    {n:4, label:"Tema"},
    {n:5, label:"Processos"},
  ];

  function renderStepper(current){
    const el = document.getElementById("stepper");
    el.innerHTML = "";
    STEPS.forEach(function(s, idx){
      const node = document.createElement("div");
      const canJump = s.n < current;
      node.className = "step-node" + (s.n === current ? " active" : "") + (s.n < current ? " done" : "") + (canJump ? " clickable" : "");
      node.innerHTML = '<span class="step-circle">'+ (s.n < current ? "✓" : s.n) +'</span><span class="step-label">'+s.label+'</span>';
      if(canJump){
        node.addEventListener("click", function(){ goTo(s.n); });
      }
      el.appendChild(node);
      if(idx < STEPS.length-1){
        const c = document.createElement("div");
        c.className = "step-connector";
        el.appendChild(c);
      }
    });
  }

  function goTo(n){
    document.querySelectorAll(".panel").forEach(function(p){
      p.classList.toggle("active", p.getAttribute("data-panel") === String(n));
    });
    renderStepper(n);
    window.scrollTo({top:0, behavior:"smooth"});
  }

  /* ---------- Step 1: Estado ---------- */
  function renderEstados(filter){
    const grid = document.getElementById("estadoGrid");
    grid.innerHTML = "";
    const f = (filter||"").toLowerCase();
    ESTADOS.filter(function(es){
      return !f || es.nome.toLowerCase().indexOf(f)!==-1 || es.uf.toLowerCase().indexOf(f)!==-1 || es.regiao.toLowerCase().indexOf(f)!==-1;
    }).forEach(function(es){
      const card = document.createElement("button");
      card.className = "card";
      card.type = "button";
      card.innerHTML = '<span class="type-badge">'+es.regiao+'</span>'+
        '<span class="card-title">'+es.nome+' <span style="color:var(--ink-faint); font-family:var(--font-mono); font-size:14px;">('+es.uf+')</span></span>'+
        '<span class="card-meta">📍 Capital: '+es.capital+'</span>';
      card.addEventListener("click", function(){ selectEstado(es); });
      grid.appendChild(card);
    });
  }

  function selectEstado(es){
    state.estado = es;
    state.tribunal = null; state.vara = null;
    document.getElementById("breadcrumbTribunal").innerHTML = '<span class="chip">'+es.nome+' ('+es.uf+')</span>';
    renderTribunaisDoEstado();
    goTo(2);
  }

  document.getElementById("estadoSearch").addEventListener("input", function(e){
    renderEstados(e.target.value);
  });

  /* ---------- Step 2: Tribunal (derivado do estado) ---------- */
  function renderTribunaisDoEstado(){
    const grid = document.getElementById("tribunalGrid");
    grid.innerHTML = "";
    tribunaisDoEstado(state.estado).forEach(function(tr){
      const card = document.createElement("button");
      card.className = "card";
      card.type = "button";
      card.innerHTML = '<span class="type-badge">'+tr.tipo+'</span>'+
        '<span class="card-title">'+tr.nome+'</span>'+
        '<span class="card-meta">'+tr.desc+'</span>';
      card.addEventListener("click", function(){ selectTribunal(tr); });
      grid.appendChild(card);
    });
  }

  function selectTribunal(tr){
    state.tribunal = tr;
    state.vara = null;
    document.getElementById("breadcrumbVara").innerHTML =
      '<span class="chip">'+state.estado.nome+' ('+state.estado.uf+')</span><span class="sep">›</span><span class="chip">'+tr.nome+'</span>';
    document.getElementById("varaSearch").value = "";
    goTo(3);
    loadVaras(tr);
  }

  /* ---------- Step 3: Vara (dados reais, ao vivo) ---------- */
  function loadVaras(tr){
    const status = document.getElementById("varaStatus");
    const grid = document.getElementById("varaGrid");
    grid.innerHTML = "";
    varasCache = null;
    status.textContent = "Carregando varas reais do " + tr.nome + " — consulta ao vivo do DataJud, pode levar alguns segundos…";
    fetchJSON(API_BASE + "/api/tribunais/" + tr.alias + "/varas")
      .then(function(data){
        varasCache = (data.varas || []).slice().sort(function(a,b){ return b.processosIndexados - a.processosIndexados; });
        status.textContent = varasCache.length + " vara(s)/órgão(s) julgador(es) encontrados" +
          (varasCache.length > 120 ? " — mostrando os 120 com mais processos indexados; use a busca para refinar." : ".");
        renderVaras("");
      })
      .catch(function(err){
        status.innerHTML = '<span style="color:var(--critical);">Erro ao carregar varas: '+err.message+'</span>';
      });
  }

  function renderVaras(filter){
    const grid = document.getElementById("varaGrid");
    grid.innerHTML = "";
    if(!varasCache) return;
    const f = (filter||"").toLowerCase();
    varasCache.filter(function(v){ return !f || v.nome.toLowerCase().indexOf(f)!==-1; })
      .slice(0,120)
      .forEach(function(v){
        const card = document.createElement("button");
        card.className = "card";
        card.type = "button";
        card.innerHTML = '<span class="type-badge">DataJud</span>'+
          '<span class="card-title">'+v.nome+'</span>'+
          '<span class="card-meta">📊 '+v.processosIndexados.toLocaleString("pt-BR")+' processos indexados</span>';
        card.addEventListener("click", function(){ selectVara(v); });
        grid.appendChild(card);
      });
  }

  document.getElementById("varaSearch").addEventListener("input", function(e){
    renderVaras(e.target.value);
  });

  function selectVara(v){
    state.vara = v;
    document.getElementById("breadcrumbTema").innerHTML =
      '<span class="chip">'+state.estado.nome+' ('+state.estado.uf+')</span><span class="sep">›</span>'+
      '<span class="chip">'+state.tribunal.nome+'</span><span class="sep">›</span><span class="chip">'+v.nome+'</span>';
    document.getElementById("temaDetected").innerHTML = "";
    document.getElementById("inicialText").value = "";
    goTo(4);
    ensureTemasLoaded();
  }

  /* ---------- Step 4: Tema (dados reais) ---------- */
  function ensureTemasLoaded(){
    if(temasCache){ populateTemaSelect(); return; }
    const sel = document.getElementById("temaSelect");
    sel.innerHTML = '<option value="">Carregando temas…</option>';
    fetchJSON(API_BASE + "/api/temas")
      .then(function(data){
        temasCache = data.temas || [];
        populateTemaSelect();
      })
      .catch(function(err){
        sel.innerHTML = '<option value="">Erro ao carregar temas</option>';
      });
  }

  function populateTemaSelect(){
    const sel = document.getElementById("temaSelect");
    sel.innerHTML = '<option value="">Selecione um tema…</option>' +
      temasCache.map(function(t){ return '<option value="'+t.id+'">'+t.nome+'</option>'; }).join("");
  }

  function detectTema(text){
    if(!temasCache) return null;
    const t = text.toLowerCase();
    let best = null, bestScore = 0;
    temasCache.forEach(function(tm){
      const kws = KW_POR_TEMA[tm.id] || [];
      let score = 0;
      kws.forEach(function(k){ if(t.indexOf(k) !== -1) score++; });
      if(score > bestScore){ bestScore = score; best = tm; }
    });
    return bestScore > 0 ? best : null;
  }

  document.getElementById("inicialText").addEventListener("input", function(e){
    const tm = detectTema(e.target.value);
    const box = document.getElementById("temaDetected");
    if(tm){
      box.innerHTML = '<div class="tema-chip">🔎 Tema identificado automaticamente: '+tm.nome+'</div>';
      document.getElementById("temaSelect").value = tm.id;
    } else if(e.target.value.trim().length > 20){
      box.innerHTML = '<div class="hint" style="margin-top:14px;">Não foi possível identificar automaticamente um tema já mapeado — selecione manualmente abaixo.</div>';
    } else {
      box.innerHTML = "";
    }
  });

  document.getElementById("analyzeBtn").addEventListener("click", function(){
    const selId = document.getElementById("temaSelect").value;
    const tema = (temasCache || []).find(function(t){ return t.id === selId; });
    if(!tema){
      const sel = document.getElementById("temaSelect");
      sel.style.borderColor = "var(--critical)";
      sel.focus();
      return;
    }
    document.getElementById("temaSelect").style.borderColor = "";
    state.tema = tema;
    state.pagina = 1;
    state.processosAcumulados = [];
    renderResultadoHeader();
    goTo(5);
    loadProcessos(false);
  });

  /* ---------- Step 5: Processos reais ---------- */
  function renderResultadoHeader(){
    document.getElementById("breadcrumbResultado").innerHTML =
      '<span class="chip">'+state.estado.nome+' ('+state.estado.uf+')</span><span class="sep">›</span>'+
      '<span class="chip">'+state.tribunal.nome+'</span><span class="sep">›</span>'+
      '<span class="chip">'+state.vara.nome+'</span><span class="sep">›</span>'+
      '<span class="chip">'+state.tema.nome+'</span>';
    document.getElementById("resultTitle").textContent = "Processos reais — " + state.vara.nome;
    document.getElementById("resultSub").textContent = state.tribunal.nome + " · " + state.tema.nome;
  }

  function loadProcessos(append){
    const status = document.getElementById("resultStatus");
    const btnMais = document.getElementById("carregarMaisBtn");
    status.textContent = "Consultando o DataJud ao vivo…";
    btnMais.disabled = true;
    const url = API_BASE + "/api/processos?tribunal=" + encodeURIComponent(state.tribunal.alias) +
      "&vara=" + encodeURIComponent(state.vara.nome) +
      "&tema=" + encodeURIComponent(state.tema.id) +
      "&pagina=" + state.pagina + "&tamanho=20";
    fetchJSON(url)
      .then(function(data){
        const novos = data.processos || [];
        state.processosAcumulados = append ? state.processosAcumulados.concat(novos) : novos;
        const linkEl = document.getElementById("consultaPublicaLink");
        linkEl.textContent = data.consultaPublica ? ("Onde consultar: " + data.consultaPublica) : "";
        renderTabelaProcessos();
        status.textContent = state.processosAcumulados.length + " processo(s) carregado(s) nesta consulta.";
        btnMais.style.display = novos.length === 20 ? "inline-flex" : "none";
        btnMais.disabled = false;
      })
      .catch(function(err){
        status.innerHTML = '<span style="color:var(--critical);">Erro ao buscar processos: '+err.message+'</span>';
        btnMais.style.display = "none";
      });
  }

  function renderTabelaProcessos(){
    const body = document.getElementById("precedentesBody");
    if(!state.processosAcumulados.length){
      body.innerHTML = '<tr><td colspan="5" style="color:var(--ink-faint);">Nenhum processo encontrado com esses filtros.</td></tr>';
      return;
    }
    body.innerHTML = state.processosAcumulados.map(function(p){
      return '<tr>'+
        '<td class="num">'+(p.numeroProcesso || "—")+'</td>'+
        '<td>'+(p.classe || "—")+'</td>'+
        '<td>'+(p.orgaoJulgador || "—")+'</td>'+
        '<td>'+fmtDataProcesso(p.dataAjuizamento)+'</td>'+
        '<td>'+(p.ultimoMovimento || "—")+'</td>'+
        '</tr>';
    }).join("");
  }

  document.getElementById("carregarMaisBtn").addEventListener("click", function(){
    state.pagina += 1;
    loadProcessos(true);
  });

  document.getElementById("resetBtn").addEventListener("click", function(){
    state.estado = null; state.tribunal = null; state.vara = null; state.tema = null;
    state.pagina = 1; state.processosAcumulados = [];
    document.getElementById("estadoSearch").value = "";
    renderEstados("");
    goTo(1);
  });

  document.querySelectorAll("[data-goto]").forEach(function(btn){
    btn.addEventListener("click", function(){ goTo(parseInt(btn.getAttribute("data-goto"),10)); });
  });

  /* ---------- Modal ---------- */
  const modal = document.getElementById("infoModal");
  function openModal(){ modal.classList.add("open"); }
  function closeModal(){ modal.classList.remove("open"); }
  document.getElementById("openInfo").addEventListener("click", openModal);
  document.getElementById("openInfo2").addEventListener("click", openModal);
  document.getElementById("closeInfo").addEventListener("click", closeModal);
  modal.addEventListener("click", function(e){ if(e.target === modal) closeModal(); });
  document.addEventListener("keydown", function(e){ if(e.key === "Escape") closeModal(); });

  /* ---------- Init ---------- */
  renderEstados("");
  renderStepper(1);
})();
</script>
"""

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

    # ---- Ampliação de 27/08 — Saúde (planos), Aéreo e Usucapião (pedido
    # prioritário) + cobertura mais ampla das demais áreas do escritório.
    # Códigos levantados no CSV oficial (assuntos.csv) e depois CONFERIDOS UM
    # A UM com contagem real via /api/debug_codigos em TJSP, TJCE, TRT7 e
    # TRT2 antes de entrar aqui — não só pela regra de "folha ativa", que
    # sozinha se mostrou insuficiente: alguns nós-pai (ex.: 7664 Dissolução,
    # 4993 Recuperação Judicial, 6017 Execução Fiscal, 13998 Multa 40% FGTS,
    # 13875/13877 Adicionais de Insalubridade/Periculosidade) são usados
    # diretamente em volume real mesmo tendo filhos ativos, e um "folha"
    # (13853, Plano de Saúde/verbas trabalhistas) devolveu 0 em ambos TJSP e
    # TJCE e por isso foi removido. Cobertura pode variar por tribunal —
    # normal, mesma lógica de qualquer tema aqui.
    "plano_saude": {
        "nome": "Plano de Saúde",
        "assuntos": [13605, 12487, 12488, 12489, 12490],
    },
    "transporte_aereo": {
        "nome": "Transporte Aéreo (Atraso, Cancelamento, Overbooking, Extravio de Bagagem, Acidente)",
        "assuntos": [4829, 4830, 4831, 4832, 7748],
    },
    "usucapiao": {
        "nome": "Usucapião",
        "assuntos": [10457, 10458, 10459, 10460, 10500, 11980, 11990],
    },
    "divorcio_uniao_estavel": {
        "nome": "Divórcio / Dissolução de União Estável",
        "assuntos": [7664, 5813, 14923, 7677, 7672, 14924, 11988],
    },
    "alimentos": {
        "nome": "Ação de Alimentos",
        "assuntos": [6239, 5787, 5788, 6238, 10859],
    },
    "guarda_visitas": {
        "nome": "Guarda e Regulamentação de Visitas",
        "assuntos": [5802, 5805, 5801, 11977],
    },
    "inventario_sucessoes": {
        "nome": "Inventário e Sucessões",
        "assuntos": [7687, 5833, 7676, 5825, 5829, 11991, 15087],
    },
    "empresarial_societario": {
        "nome": "Direito Societário (Constituição, Dissolução, Apuração de Haveres)",
        "assuntos": [4934, 4935, 4933, 4940, 4939, 4942, 4943],
    },
    "recuperacao_judicial_falencia": {
        "nome": "Recuperação Judicial e Falência",
        "assuntos": [4993, 4994, 4998, 5000, 5001, 9556, 9558, 9559],
    },
    "tributario_geral": {
        "nome": "Tributário (Execução Fiscal, ISS, IPTU, ITBI, ITCD)",
        "assuntos": [6017, 5951, 5952, 5954, 5955],
    },
    "posse_propriedade": {
        "nome": "Posse e Propriedade Imobiliária (Reintegração, Reivindicação, Condomínio, Incorporação)",
        "assuntos": [10445, 10446, 10447, 10452, 10450, 10462, 10470, 11000, 11001],
    },
    "contratos_civis_consumo": {
        "nome": "Contratos Civis e de Consumo (Compra e Venda, Prestação de Serviços)",
        "assuntos": [9587, 9596],
    },
    "rescisao_trabalhista": {
        "nome": "Rescisão Contratual Trabalhista (Indireta, Justa Causa, Multas CLT)",
        "assuntos": [13968, 13962, 13999, 14000, 13995, 13996, 13997],
    },
    "horas_extras_jornada": {
        "nome": "Horas Extras e Jornada de Trabalho",
        "assuntos": [13787, 13796, 13797, 13791, 13792],
    },
    "fgts_trabalhista": {
        "nome": "FGTS (Multa de 40%, Depósito, Diferenças, Correção, Levantamento)",
        "assuntos": [13998, 13748, 13749, 13750],
    },
    "assedio_trabalho": {
        "nome": "Assédio Moral e Sexual no Trabalho",
        "assuntos": [14018, 14019],
    },
    "equiparacao_salarial": {
        "nome": "Equiparação Salarial / Isonomia",
        "assuntos": [13420, 13693, 14044],
    },
    "desvio_acumulo_funcao": {
        "nome": "Desvio e Acúmulo de Função",
        "assuntos": [13732, 13733, 13922],
    },
    "dano_moral_trabalhista": {
        "nome": "Indenização por Dano Moral (Trabalhista)",
        "assuntos": [14011, 14033],
    },
    "adicional_insalubridade_periculosidade": {
        "nome": "Adicional de Insalubridade e Periculosidade",
        "assuntos": [13875, 13877],
    },
}

app = FastAPI(title="Prognose Jurídica — backend DataJud")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrinja ao domínio real do frontend antes de ir para produção
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def frontend():
    return FRONTEND_HTML


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


@app.get("/api/debug_codigos")
def debug_codigos(
    tribunal: str = Query(..., description="Alias do tribunal, ex.: TJSP"),
    codigos: str = Query(..., description="Códigos de assunto separados por vírgula, ex.: 13605,13853"),
):
    """Endpoint de apoio para conferir, num único request, quantos processos
    reais existem para cada código de assunto candidato — a mesma checagem
    manual que revelou o bug do dano_moral (nó-pai sempre 0), agora sem
    precisar rodar nada localmente. Não é usado pelo frontend."""
    tribunal = tribunal.upper()
    if tribunal not in TRIBUNAL_ALIASES:
        raise HTTPException(status_code=404, detail=f"Tribunal '{tribunal}' não configurado.")
    endpoint = f"api_publica_{TRIBUNAL_ALIASES[tribunal]}"
    try:
        codigos_lista = [int(c.strip()) for c in codigos.split(",") if c.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="Parâmetro 'codigos' deve ser uma lista de inteiros separados por vírgula.")

    body = {
        "size": 0,
        "query": {"terms": {"assuntos.codigo": codigos_lista}},
        "aggs": {"por_codigo": {"terms": {"field": "assuntos.codigo", "size": 1000}}},
    }
    data = _post_datajud(endpoint, body)
    buckets = data.get("aggregations", {}).get("por_codigo", {}).get("buckets", [])
    contagens = {b["key"]: b["doc_count"] for b in buckets}
    return {
        "tribunal": tribunal,
        "contagens": {c: contagens.get(c, 0) for c in codigos_lista},
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}
