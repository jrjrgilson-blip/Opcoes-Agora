"""
Consulta rápida de opções — interface Streamlit.

Reaproveita as funções de selecionar_opcoes.py; toda a lógica de vencimento
mensal, faixa em torno do dinheiro e separação entre cotação e fechamento
vive lá, para a versão web e a de linha de comando nunca divergirem.

Rodar local:
    streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import streamlit as st

import selecionar_opcoes as so

ATALHOS = ["PETR4", "VALE3", "BBAS3", "ITUB4", "BBDC4"]

st.set_page_config(page_title="Opções no dinheiro", page_icon="📊",
                   layout="wide", initial_sidebar_state="collapsed")


def obter_token() -> str | None:
    """Secrets do Streamlit em produção, variável de ambiente no local."""
    try:
        if "BRAPI_TOKEN" in st.secrets:
            return st.secrets["BRAPI_TOKEN"]
    except Exception:
        pass
    return os.environ.get("BRAPI_TOKEN")


@st.cache_resource
def cliente(token: str | None) -> so.BrapiClient:
    return so.BrapiClient(token=token)


# A cotação vale pouco tempo; a cadeia é do último pregão e não muda no dia.
# TTLs diferentes evitam repetir chamadas sem deixar o preço envelhecer.
@st.cache_data(ttl=45, show_spinner=False)
def buscar_spot(ativo: str, token: str | None) -> tuple[float | None, str]:
    return so.spot_da_cotacao(cliente(token), ativo)


@st.cache_data(ttl=900, show_spinner=False)
def buscar_cadeia(ativo: str, token: str | None,
                  hoje_iso: str) -> tuple[pd.DataFrame, list[str], float | None]:
    cli = cliente(token)
    hoje = date.fromisoformat(hoje_iso)
    vencs = so.parse_expirations(cli.expirations(ativo))
    alvos = so.vencimentos_mensais(vencs, hoje)
    partes, fechamento = [], None
    for exp in alvos:
        df = so.normalize(cli.chain(ativo, exp), underlying=ativo,
                          expiration=exp, trade_date=hoje_iso)
        if df.empty:
            continue
        if fechamento is None and "spot_payload" in df.columns:
            v = pd.to_numeric(df["spot_payload"], errors="coerce").dropna()
            fechamento = float(v.iloc[0]) if not v.empty else None
        partes.append(df)
    if not partes:
        return pd.DataFrame(), alvos, fechamento
    return pd.concat(partes, ignore_index=True), alvos, fechamento


st.title("Opções em torno do dinheiro")

token = obter_token()
if not token:
    st.warning("Sem BRAPI_TOKEN configurado — apenas PETR4, VALE3, ITUB4 e "
               "MGLU3 respondem (sandbox).")

col_a, col_b = st.columns([2, 3])
with col_a:
    ativo = st.text_input("Ativo", value=st.session_state.get("ativo", "PETR4"),
                          max_chars=10).strip().upper()
with col_b:
    st.caption("Atalhos")
    for coluna, t in zip(st.columns(len(ATALHOS)), ATALHOS):
        if coluna.button(t, use_container_width=True):
            st.session_state["ativo"] = t
            st.rerun()

c1, c2, c3 = st.columns(3)
faixa = c1.slider("Faixa em torno do preço (%)", 2, 30, 10) / 100
min_neg = c2.number_input("Mínimo de negócios", 0, 10_000, 0, step=10)
estilo = c3.selectbox("Estilo", ["todos", "american", "european"],
                      help="Na B3 as calls são americanas e as puts europeias — "
                           "filtrar por estilo elimina um dos lados.")

if not ativo:
    st.stop()

hoje = date.today()
try:
    with st.spinner(f"Consultando {ativo}…"):
        spot, hora = buscar_spot(ativo, token)
        df, alvos, fechamento = buscar_cadeia(ativo, token, hoje.isoformat())
except so.BrapiError as exc:
    st.error(f"A API recusou a consulta: {exc}")
    st.stop()

if df.empty:
    st.error(f"Nenhuma série retornada para {ativo}. "
             "Verifique o código do ativo ou se o seu plano cobre opções dele.")
    st.stop()

origem = f"cotação de {hora}" if spot else "fechamento do último pregão"
if spot is None:
    spot = fechamento
if not spot:
    st.error("Não obtive o preço do ativo — sem ele não há linha do dinheiro.")
    st.stop()

m1, m2 = st.columns([1, 3])
deriva = (spot / fechamento - 1) if fechamento else None
m1.metric(ativo, f"{spot:.2f}",
          f"{deriva:+.2%} vs fechamento" if deriva else None)
m2.caption(f"Preço do ativo: {origem}. "
           f"Preço, volume e negócios das opções: último pregão fechado"
           f"{f' ({fechamento:.2f})' if fechamento else ''}. "
           f"Vencimentos: {', '.join(alvos)}.")

if deriva is not None and abs(deriva) > 0.03:
    st.warning(f"O ativo andou {deriva:+.2%} desde o fechamento em que os "
               f"prêmios abaixo foram apurados. Confira o preço na corretora "
               f"antes de enviar ordem.")

df = so.enrich(df, spot)
venc = pd.to_datetime(df["expiration"], errors="coerce", format="mixed")
df["dte"] = (venc - pd.Timestamp(hoje)).dt.days

if estilo != "todos" and "optionStyle" in df.columns:
    df = df[df["optionStyle"].astype(str).str.lower() == estilo]
if min_neg and "trades" in df.columns:
    df = df[pd.to_numeric(df["trades"], errors="coerce").fillna(0) >= min_neg]

tab = so.montar_tabela(df, spot, faixa)
if tab.empty:
    st.info("Nenhuma série dentro dos filtros. Aumente a faixa ou reduza o "
            "mínimo de negócios.")
    st.stop()

COLUNAS = {
    "symbol": st.column_config.TextColumn("série", width="small"),
    "strike": st.column_config.NumberColumn("strike", format="%.2f"),
    "dist": st.column_config.NumberColumn("dist", format="%+.1f%%",
                                          help="distância do strike ao preço"),
    "price": st.column_config.NumberColumn("prêmio", format="%.2f"),
    "var": st.column_config.NumberColumn("var", format="%+.1f%%",
                                         help="variação no último pregão"),
    "volume": st.column_config.NumberColumn("volume", format="%d"),
    "trades": st.column_config.NumberColumn("negócios", format="%d"),
    "open_interest": st.column_config.NumberColumn("em aberto", format="%d"),
    "optionStyle": st.column_config.TextColumn("estilo", width="small"),
}

for exp, g in tab.groupby("expiration"):
    d = g["dte"].dropna()
    dias = int(d.iloc[0]) if not d.empty else 0
    st.subheader(f"Vencimento {str(exp)[:10]} · {dias} dias")
    lado_c, lado_p = st.columns(2)
    for coluna, lado in ((lado_c, "call"), (lado_p, "put")):
        sub = g[g["side"] == lado].drop(columns=["side", "expiration", "dte"],
                                        errors="ignore")
        with coluna:
            st.markdown(f"**{lado.upper()}S** · {len(sub)} séries")
            if sub.empty:
                st.caption("nenhuma série nos filtros")
                continue
            vis = sub.copy()
            for c in ("dist", "var"):
                if c in vis.columns:
                    vis[c] = pd.to_numeric(vis[c], errors="coerce") * 100
            st.dataframe(
                vis, hide_index=True, use_container_width=True,
                column_config={k: v for k, v in COLUNAS.items() if k in vis.columns},
            )

st.divider()
st.caption("Dados: brapi.dev. Ferramenta de triagem — serve para chegar na "
           "corretora sabendo quais séries olhar, não para preço de execução.")
