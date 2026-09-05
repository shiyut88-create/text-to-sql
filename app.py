# ========== 编码修复 ==========
import os
import sys
import re
from dotenv import load_dotenv
load_dotenv()

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONLEGACYWINDOWSSTDIO"] = "utf-8"
os.environ["HTTPX_DEFAULT_ENCODING"] = "utf-8"
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# ========== 正式导入 ==========
import streamlit as st
import sqlite3
import pandas as pd
from openai import OpenAI
import plotly.express as px
import tempfile
import io

# ========== 页面设置 ==========
st.set_page_config(page_title="Text-to-SQL 查询系统", page_icon="🔍")
st.title("🔍 自然语言数据库查询系统")
st.caption("上传任意 CSV 文件，用中文提问自动查询")

# ========== 初始化客户端 ==========
@st.cache_resource
def load_client():
    return OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com/v1"
    )

client = load_client()

# ========== 安全处理：表名防注入 ==========
def sanitize_table_name(name):
    """将文件名净化为安全的 SQL 表名"""
    # 去掉扩展名，转小写
    name = name.lower()
    # 只保留字母、数字、下划线
    name = re.sub(r'[^a-z0-9_]', '_', name)
    # 不能以数字开头
    if name and name[0].isdigit():
        name = 't_' + name
    # 避开 SQL 关键字
    keywords = {'select', 'from', 'where', 'table', 'index', 'order', 'group',
                'by', 'join', 'create', 'drop', 'insert', 'delete', 'update',
                'values', 'default', 'primary', 'foreign', 'key', 'check'}
    if name in keywords:
        name = name + '_table'
    # 不能为空
    if not name:
        name = 'data_table'
    return name

# ========== Session 初始化 ==========
if "history" not in st.session_state:
    st.session_state.history = []
if "db_path" not in st.session_state:
    st.session_state.db_path = None
if "schema" not in st.session_state:
    st.session_state.schema = None
if "table_names" not in st.session_state:
    st.session_state.table_names = []
if "overview_data" not in st.session_state:
    st.session_state.overview_data = {}
if "suggested_questions" not in st.session_state:
    st.session_state.suggested_questions = []
if "clicked_question" not in st.session_state:
    st.session_state.clicked_question = None

# ========== 自动生成 Schema（带统计信息）==========
def generate_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    tables = cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    schema = ""
    table_names = []
    overview = {}
    
    for (table_name,) in tables:
        table_names.append(table_name)
        
        # 获取列信息
        columns = cursor.execute(f"PRAGMA table_info({table_name})").fetchall()
        schema += f"\n表名：{table_name}\n"
        schema += "列信息：\n"
        
        df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
        overview[table_name] = {
            "df": df,
            "rows": len(df),
            "cols": len(df.columns),
            "missing": df.isnull().sum().to_dict(),
            "dtypes": df.dtypes.astype(str).to_dict()
        }
        
        for col in df.columns:
            dtype = str(df[col].dtype)
            null_count = int(df[col].isnull().sum())
            unique_count = int(df[col].nunique())
            
            schema += f"  - {col}({dtype})"
            
            # 数值列：给分布范围
            if pd.api.types.is_numeric_dtype(df[col]) and not df[col].isnull().all():
                min_v = df[col].min()
                max_v = df[col].max()
                mean_v = df[col].mean()
                schema += f", 范围[{min_v:.2f} ~ {max_v:.2f}], 均值{mean_v:.2f}"
            # 枚举值较少的文本列：给取值示例
            elif unique_count <= 10 and unique_count > 0:
                samples = df[col].dropna().unique()[:5]
                schema += f", 取值示例: {list(samples)}"
            
            schema += f", 缺失{null_count}条, 唯一值{unique_count}个\n"
        
        # 示例数据
        sample = df.head(3).to_dict(orient='records')
        schema += f"示例数据：{sample}\n"
    
    conn.close()
    
    # 限制 schema 长度，避免超出模型上下文
    if len(schema) > 6000:
        schema = schema[:6000] + "\n...（表结构描述过长，已截断）"
    
    return schema, table_names, overview

# ========== 自动生成示例问题 ==========
def generate_suggested_questions(schema):
    prompt = f"""根据以下数据库结构，生成5个有价值的中文查询问题，要具体、实用、涵盖不同分析角度。
只返回5个问题，每行一个，不要编号，不要其他文字。

数据库结构：
{schema}"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )
    questions = response.choices[0].message.content.strip().split("\n")
    return [q.strip() for q in questions if q.strip()][:5]

# ========== 生成 SQL（支持多表 JOIN）==========
def generate_sql(question, schema, history):
    history_text = ""
    for record in history[-3:]:
        history_text += f"用户问：{record['question']}\n"
        if record['result'] is not None:
            history_text += f"查询结果：{record['result'].to_string(index=False, max_rows=5)}\n\n"

    prompt = f"""你是一个 SQL 专家，根据用户的自然语言问题生成对应的 SQLite SQL 查询语句。

数据库结构如下：
{schema}
{"历史对话记录：" + history_text if history_text else ""}

注意事项：
1. 只返回 SQL 语句，不要其他文字
2. 使用 SQLite 语法
3. 数据库中有多个表时，如果问题涉及跨表分析，请通过 JOIN 进行关联查询；关联条件优先选择列名相似或语义相关的列（如 user_id = id、订单.用户ID = 用户.ID 等）
4. 适当使用 LIMIT 避免返回太多数据（默认限制20条）
5. 涉及金额的查询用 SUM、AVG 等聚合函数
6. 如果用户在追问上一个结果中的具体内容，请基于历史查询结果中的数据来生成 SQL

用户问题：{question}

SQL："""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    sql = response.choices[0].message.content.strip()
    sql = sql.replace("```sql", "").replace("```", "").strip()
    return sql

# ========== 执行 SQL ==========
def run_sql(sql, db_path):
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(sql, conn)
        return df, None
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()

# ========== SQL 错误自动修复（最多重试2次）==========
def fix_sql(question, sql, error, schema):
    prompt = f"""你是一个 SQL 专家，以下 SQL 语句执行出错，请修复它。

数据库结构：
{schema}

用户问题：{question}

错误的 SQL：
{sql}

错误信息：
{error}

请返回修复后的 SQL，只返回 SQL 语句，不要其他文字。"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    fixed_sql = response.choices[0].message.content.strip()
    fixed_sql = fixed_sql.replace("```sql", "").replace("```", "").strip()
    return fixed_sql

# ========== 自动图表 ==========
def auto_chart(df):
    if df is None or len(df) == 0:
        return
    cols = df.columns.tolist()
    num_cols = df.select_dtypes(include='number').columns.tolist()
    if len(cols) == 2 and len(num_cols) == 1:
        label_col = [c for c in cols if c not in num_cols][0]
        value_col = num_cols[0]
        if len(df) <= 8:
            fig = px.pie(df, names=label_col, values=value_col, title="数据分布")
        else:
            fig = px.bar(df, x=label_col, y=value_col, title="数据对比")
        st.plotly_chart(fig, use_container_width=True)

# ========== 自然语言总结 ==========
def generate_summary(question, df):
    if df is None or len(df) == 0:
        return "未查询到相关数据。"
    data_str = df.to_string(index=False, max_rows=10)
    prompt = f"""用户问题：{question}
查询结果：
{data_str}

请用一句简洁的中文总结这个查询结果，直接说结论，不要废话。"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    return response.choices[0].message.content.strip()

# ========== 导出查询历史为 Excel ==========
def export_history_excel(history):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary_rows = []
        for i, record in enumerate(history):
            summary_rows.append({
                "序号": i + 1,
                "问题": record["question"],
                "生成的SQL": record["sql"],
                "是否自动修复": "是" if record.get("auto_fixed") else "否",
                "总结": record.get("summary", ""),
                "是否出错": "是" if record["error"] else "否"
            })
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="查询摘要", index=False)

        for i, record in enumerate(history):
            if record["result"] is not None and not record["error"]:
                sheet_name = f"查询{i+1}"[:31]
                record["result"].to_excel(writer, sheet_name=sheet_name, index=False)

    buf.seek(0)
    return buf

# ========== 侧边栏 ==========
with st.sidebar:
    st.header("📂 上传数据")
    uploaded_files = st.file_uploader(
        "上传 CSV 文件（支持多个）",
        type=["csv"],
        accept_multiple_files=True
    )

    if uploaded_files:
        if st.button("🔄 构建数据库"):
            with st.spinner("正在构建数据库..."):
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
                db_path = tmp.name
                tmp.close()

                conn = sqlite3.connect(db_path)
                for f in uploaded_files:
                    # 使用安全净化后的表名
                    table_name = sanitize_table_name(os.path.splitext(f.name)[0])
                    df = pd.read_csv(f)
                    # 列名也做安全处理
                    df.columns = [re.sub(r'[^a-zA-Z0-9_]', '_', c.lower().strip()) for c in df.columns]
                    df.to_sql(table_name, conn, if_exists="replace", index=False)
                    st.write(f"✔ 已导入：{table_name}（{len(df)} 条）")
                conn.close()

                st.session_state.db_path = db_path
                st.session_state.schema, st.session_state.table_names, st.session_state.overview_data = generate_schema(db_path)
                st.session_state.history = []

                with st.spinner("正在生成示例问题..."):
                    st.session_state.suggested_questions = generate_suggested_questions(st.session_state.schema)

                st.success("✅ 数据库构建完成！")

    if st.session_state.table_names:
        st.divider()
        st.header("📋 已加载的表")
        for t in st.session_state.table_names:
            st.write(f"• {t}")

    st.divider()
    st.header("📥 导出")
    if st.session_state.history:
        excel_buf = export_history_excel(st.session_state.history)
        st.download_button(
            label="📊 导出查询历史为 Excel",
            data=excel_buf,
            file_name="查询历史.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.caption("暂无查询记录")

    st.divider()
    if st.button("🗑️ 清空记录"):
        st.session_state.history = []
        st.rerun()

# ========== 主界面 ==========
if not st.session_state.db_path:
    st.info("👈 请先在左侧上传 CSV 文件并点击构建数据库")
else:
    tab1, tab2 = st.tabs(["📊 数据概览", "💬 自然语言查询"])

    with tab1:
        st.markdown("### 📋 数据表概览")
        for table_name, info in st.session_state.overview_data.items():
            with st.expander(f"📄 {table_name}  （{info['rows']} 行 × {info['cols']} 列）"):
                col1, col2, col3 = st.columns(3)
                col1.metric("总行数", info['rows'])
                col2.metric("总列数", info['cols'])
                col3.metric("缺失值列数", sum(1 for v in info['missing'].values() if v > 0))

                st.markdown("**列信息**")
                dtype_df = pd.DataFrame({
                    "列名": list(info['dtypes'].keys()),
                    "数据类型": list(info['dtypes'].values()),
                    "缺失值数量": [info['missing'].get(c, 0) for c in info['dtypes'].keys()]
                })
                st.dataframe(dtype_df, use_container_width=True)

                st.markdown("**前5行数据预览**")
                st.dataframe(info['df'].head(5), use_container_width=True)

    with tab2:
        if st.session_state.suggested_questions:
            st.markdown("**💡 推荐问题（点击直接提问）**")
            cols = st.columns(len(st.session_state.suggested_questions))
            for i, q in enumerate(st.session_state.suggested_questions):
                if cols[i].button(q, key=f"sq_{i}"):
                    st.session_state.clicked_question = q
                    st.rerun()
            st.divider()

        for record in st.session_state.history:
            with st.chat_message("user"):
                st.write(record["question"])
            with st.chat_message("assistant"):
                st.code(record["sql"], language="sql")
                if record.get("auto_fixed"):
                    st.warning("⚠️ 首次生成的 SQL 出错，已自动修复并重新查询")
                if record["error"]:
                    st.error(f"查询出错：{record['error']}")
                elif record["result"] is not None:
                    st.dataframe(record["result"])
                    auto_chart(record["result"])
                    st.info(f"💡 {record['summary']}")

        question = st.chat_input("用中文提问，例如：销售额最高的品类是什么？")
        if st.session_state.clicked_question:
            question = st.session_state.clicked_question
            st.session_state.clicked_question = None

        if question:
            with st.chat_message("user"):
                st.write(question)

            with st.chat_message("assistant"):
                with st.spinner("正在生成 SQL..."):
                    sql = generate_sql(question, st.session_state.schema, st.session_state.history)
                st.code(sql, language="sql")

                with st.spinner("正在查询数据库..."):
                    df, error = run_sql(sql, st.session_state.db_path)

                auto_fixed = False

                # 自动修复：最多重试2次
                retry_count = 0
                while error and retry_count < 2:
                    with st.spinner(f"SQL 出错，第{retry_count + 1}次自动修复..."):
                        fixed_sql = fix_sql(question, sql, error, st.session_state.schema)
                        df, error = run_sql(fixed_sql, st.session_state.db_path)
                        if not error:
                            sql = fixed_sql
                            auto_fixed = True
                            st.warning("⚠️ 首次生成的 SQL 出错，已自动修复并重新查询")
                            break
                        retry_count += 1

                if error:
                    st.error(f"查询出错：{error}")
                    summary = "查询出错，请换一种问法试试。"
                else:
                    st.dataframe(df)
                    auto_chart(df)
                    with st.spinner("正在生成总结..."):
                        summary = generate_summary(question, df)
                    st.info(f"💡 {summary}")

                st.session_state.history.append({
                    "question": question,
                    "sql": sql,
                    "result": df,
                    "error": error,
                    "summary": summary,
                    "auto_fixed": auto_fixed
                })