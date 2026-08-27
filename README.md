# Backend Prognose Jurídica — DataJud (deploy no Vercel)

Proxy real para a API Pública do DataJud (CNJ), pronto para rodar como Vercel
Functions em Python. Implementa os 3 endpoints da seção 4 da "Arquitetura
DataJud v2": `/api/tribunais/{alias}/varas`, `/api/temas`, `/api/processos`.

## Estrutura desta pasta

```
app.py             ← toda a lógica do backend (FastAPI), um único arquivo
requirements.txt   ← dependências Python (fastapi, requests)
```

Note que **não há mais pasta `api/` nem `vercel.json`** — essa foi a causa do
404 na primeira tentativa de deploy (ver "O que deu errado" abaixo). O
FastAPI já define suas próprias rotas com o prefixo `/api/...` dentro do
código (`app.py`); não precisa de nenhuma configuração de rota por fora.

## Como corrigir o repositório que já existe

Se você já tem o repositório `BuscaPorTemas` no GitHub com a estrutura antiga
(`api/index.py` + `vercel.json`), o ajuste é:

1. **Apague** a pasta `api/` e o arquivo `vercel.json` do repositório.
2. **Adicione** este `app.py` na raiz (mesmo nível de `requirements.txt` e
   `README.md`).
3. Commit e push. Como o repositório já está linkado ao Vercel, o push já
   dispara um novo deploy sozinho.
4. Depois do deploy, teste:
   ```
   https://busca-por-temas.vercel.app/api/health
   https://busca-por-temas.vercel.app/api/temas
   https://busca-por-temas.vercel.app/api/tribunais/TJSP/varas
   ```

## O que deu errado na primeira tentativa

O Vercel tem duas formas de rodar Python: um **preset de framework** (detecta
FastAPI/Flask/Django a partir de um `app.py`/`index.py` na **raiz** do
projeto, e aí a aplicação responde em qualquer caminho) e uma convenção mais
antiga de **funções por arquivo dentro de `/api`** (cada `.py` vira uma
function separada, servida no caminho do próprio arquivo). Colocamos
`index.py` dentro de `api/` — isso cai na convenção antiga, e nela
`api/index.py` é servido em `/api`, não em `/api/index`. Nosso `vercel.json`
apontava o rewrite para `/api/index`, um caminho que não existia — daí o 404
em tudo, inclusive na raiz. Mover o entrypoint para a raiz do projeto (como
está agora) elimina essa ambiguidade: é exatamente o padrão que a própria
documentação do Vercel usa nos exemplos de FastAPI.

## Por que Vercel funciona aqui

- Runtime Python 3.12 nativo (FastAPI/Flask/Django detectados automaticamente
  a partir do `requirements.txt`) — não precisa reescrever nada em Node.
- Vercel Functions fazem chamadas HTTP de saída normalmente (é uma função de
  nuvem padrão, sem allowlist de domínio) — isso é o que faltou nos dois
  ambientes onde testamos até agora (o sandbox do Claude e a VM do Cowork).
- Plano Hobby (grátis): 300s de timeout por execução, 2 GB de memória — muito
  acima do que uma consulta ao DataJud (mesmo com retentativas) deveria usar.
- Cache: em vez de subir um Redis, os endpoints devolvem `Cache-Control:
  s-maxage=...`, e o CDN do próprio Vercel cacheia a resposta. Só vale trocar
  por um Redis do Marketplace (ex. Upstash) se o uso crescer a ponto de
  precisar invalidar cache manualmente ou compartilhar entre regiões.
- `Services`, um recurso do Vercel, permite colocar este backend Python e o
  frontend (o HTML/CSS/JS do protótipo) no mesmo projeto, servidos em um
  domínio só — próximo passo natural, ainda não feito aqui.

## Testar localmente

```bash
pip install -r requirements.txt
pip install "uvicorn[standard]"   # só para rodar localmente; o Vercel não precisa disso
uvicorn app:app --reload
```

Depois:

```bash
curl "http://localhost:8000/api/tribunais/TJSP/varas"
curl "http://localhost:8000/api/processos?tribunal=TJSP&vara=1ª%20Vara%20Cível&tema=dano_moral"
```

Se o computador tiver internet normal (sem a restrição que vimos no sandbox
do Claude), isso já deve devolver dados reais do DataJud.

## Deploy alternativo via CLI (se não quiser ir pelo Git)

```bash
npm i -g vercel      # se ainda não tiver a CLI
cd vercel-backend
vercel               # segue o fluxo interativo, cria o projeto
vercel --prod        # publica em produção
```

Como o seu repositório já está linkado ao Vercel, normalmente você não precisa
disso — só commitar e dar push (seção acima) já é suficiente.

Opcional: defina `DATAJUD_APIKEY` como variável de ambiente do projeto na
Vercel (Project Settings → Environment Variables) se algum dia precisar trocar
a chave pública sem reeditar o código — o app já lê de lá com fallback para a
chave publicada pelo CNJ.

## O que falta depois disso

1. Rodar e confirmar que os endpoints devolvem dados reais (local ou já
   deployado).
2. Apontar o protótipo (`index.html`) para estes endpoints em vez do gerador
   de dados fictícios — troca pontual na etapa final do fluxo, não um
   redesenho.
3. Resolver os temas ainda pendentes (`plano_saude`, `revisao_contratual`,
   `rescisao_indireta`, `usucapiao`) usando `buscar_assunto()` de
   `datajud_client.py` antes de adicioná-los a `TEMAS_VERIFICADOS` aqui.
