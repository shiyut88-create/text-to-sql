import os
import re
import json
import sqlite3
import pandas as pd
import streamlit as st
from openai import OpenAI
import plotly.express as px
import tempfile
import io
from dotenv import load_dotenv


load_dotenv()


# ========== 页面设置 ==========
st.set_page_config(
    page_title="Text-to-SQL 查询系统",
    page_icon="🔍"
)

st.title("🔍 自然语言数据库查询系统")
st.caption("上传 CSV / Excel 文件，用中文提问自动查询")


# ========== 初始化客户端 ==========
@st.cache_resource
def load_client():

    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        st.error(
            "❌ 未检测到 DEEPSEEK_API_KEY，请检查 .env 文件。"
        )
        st.stop()

    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com/v1"
    )


client = load_client()


# ========== 安全处理：表名 ==========
def sanitize_table_name(name):

    name = name.lower()

    name = re.sub(
        r'[^a-z0-9_]',
        '_',
        name
    )

    if name and name[0].isdigit():
        name = 't_' + name

    keywords = {
        'select',
        'from',
        'where',
        'table',
        'index',
        'order',
        'group',
        'by',
        'join',
        'create',
        'drop',
        'insert',
        'delete',
        'update',
        'values',
        'default',
        'primary',
        'foreign',
        'key',
        'check'
    }

    if name in keywords:
        name = name + '_table'

    if not name:
        name = 'data_table'

    return name


# ========== 安全处理：列名 ==========
def sanitize_column_name(name):

    name = str(name).lower().strip()

    name = re.sub(
        r'[^a-zA-Z0-9_]',
        '_',
        name
    )

    if not name:
        name = "column"

    if name[0].isdigit():
        name = "col_" + name

    return name


# ========== SQL 安全检查 ==========
def validate_sql(sql):

    if not sql:
        return False, "SQL 语句为空。"

    sql = sql.strip()

    sql_without_comments = re.sub(
        r'--.*?$',
        '',
        sql,
        flags=re.MULTILINE
    )

    sql_without_comments = re.sub(
        r'/\*.*?\*/',
        '',
        sql_without_comments,
        flags=re.DOTALL
    )

    sql_without_comments = sql_without_comments.strip()

    if not sql_without_comments:
        return False, "SQL 语句为空。"

    if not re.match(
        r'^(SELECT|WITH)\b',
        sql_without_comments,
        re.IGNORECASE
    ):
        return (
            False,
            "为了安全，系统只允许执行 SELECT 查询语句。"
        )

    semicolon_count = sql_without_comments.count(";")

    if semicolon_count > 1:
        return (
            False,
            "检测到多条 SQL 语句，已拒绝执行。"
        )

    if semicolon_count == 1:

        if not sql_without_comments.rstrip().endswith(";"):
            return (
                False,
                "检测到非法的多语句 SQL，已拒绝执行。"
            )

        sql_without_comments = (
            sql_without_comments.rstrip()[:-1].strip()
        )

    dangerous_keywords = [
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "REPLACE",
        "TRUNCATE",
        "ATTACH",
        "DETACH",
        "VACUUM",
        "PRAGMA",
        "REINDEX"
    ]

    for keyword in dangerous_keywords:

        pattern = rf'\b{keyword}\b'

        if re.search(
            pattern,
            sql_without_comments,
            re.IGNORECASE
        ):
            return (
                False,
                f"检测到禁止执行的 SQL 操作：{keyword}"
            )

    return True, ""


# ========== SQLite 底层只读权限 ==========
def sqlite_read_only_authorizer(
    action,
    arg1,
    arg2,
    db_name,
    trigger_name
):

    forbidden_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,

        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,

        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,

        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_PRAGMA
    }

    if action in forbidden_actions:
        return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_OK


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


# ========== 自动生成 Schema ==========
def generate_schema(db_path):

    conn = sqlite3.connect(db_path)

    cursor = conn.cursor()

    tables = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    schema = ""

    table_names = []

    overview = {}

    for (table_name,) in tables:

        table_names.append(table_name)

        schema += f"\n表名：{table_name}\n"
        schema += "列信息：\n"

        df = pd.read_sql_query(
            f"SELECT * FROM {table_name}",
            conn
        )

        overview[table_name] = {
            "df": df,
            "rows": len(df),
            "cols": len(df.columns),
            "missing": df.isnull().sum().to_dict(),
            "dtypes": df.dtypes.astype(str).to_dict()
        }

        for col in df.columns:

            dtype = str(df[col].dtype)

            null_count = int(
                df[col].isnull().sum()
            )

            unique_count = int(
                df[col].nunique()
            )

            schema += f"  - {col}({dtype})"

            if (
                pd.api.types.is_numeric_dtype(df[col])
                and not df[col].isnull().all()
            ):

                min_v = df[col].min()
                max_v = df[col].max()
                mean_v = df[col].mean()

                schema += (
                    f", 范围[{min_v:.2f} ~ {max_v:.2f}]"
                    f", 均值{mean_v:.2f}"
                )

            elif (
                unique_count <= 10
                and unique_count > 0
            ):

                samples = (
                    df[col]
                    .dropna()
                    .unique()[:5]
                )

                schema += (
                    f", 取值示例: {list(samples)}"
                )

            schema += (
                f", 缺失{null_count}条"
                f", 唯一值{unique_count}个\n"
            )

        sample = df.head(3).to_dict(
            orient="records"
        )

        schema += (
            f"示例数据：{sample}\n"
        )

    conn.close()

    if len(schema) > 6000:

        schema = (
            schema[:6000]
            + "\n...（表结构描述过长，已截断）"
        )

    return (
        schema,
        table_names,
        overview
    )


# ========== 自动生成示例问题 ==========
def generate_suggested_questions(schema):

    prompt = f"""
根据以下数据库结构，生成5个有价值的中文查询问题。

要求：
1. 问题具体、实用
2. 尽量覆盖不同分析角度
3. 可以包含统计、排序、分类、趋势等问题
4. 只返回5个问题
5. 每行一个问题
6. 不要编号
7. 不要其他解释

数据库结构：

{schema}
"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.7
    )

    questions = (
        response
        .choices[0]
        .message
        .content
        .strip()
        .split("\n")
    )

    return [
        q.strip()
        for q in questions
        if q.strip()
    ][:5]


# ========== 生成 SQL ==========
def generate_sql(
    question,
    schema,
    history
):

    history_text = ""

    for record in history[-3:]:

        history_text += (
            f"用户问：{record['question']}\n"
        )

        if record["result"] is not None:

            history_text += (
                "查询结果："
                + record["result"]
                .to_string(
                    index=False,
                    max_rows=5
                )
                + "\n\n"
            )

    prompt = f"""
你是一个 SQL 专家。

根据用户的自然语言问题，
生成对应的 SQLite 查询语句。

数据库结构如下：

{schema}

{"历史对话记录：" + history_text if history_text else ""}

重要安全要求：

1. 只能生成 SELECT 查询语句。
2. 如果使用 CTE，只能使用 WITH ... SELECT。
3. 严禁生成 INSERT、UPDATE、DELETE、DROP、
   CREATE、ALTER、REPLACE、ATTACH、DETACH、
   PRAGMA、VACUUM 等语句。
4. 不允许修改数据库中的任何数据。
5. 只返回 SQL 语句，不要解释。
6. 使用 SQLite 语法。
7. 数据库中有多个表时，如果问题涉及跨表分析，
   可以通过 JOIN 进行关联查询。
8. 关联条件优先选择列名相似或语义相关的列。
9. 适当使用 LIMIT，默认限制20条。
10. 涉及金额、销量、利润等统计时，
    可以使用 SUM、AVG、COUNT 等聚合函数。
11. 如果用户是在追问之前的问题，
    可以参考历史查询结果。

用户问题：

{question}

SQL：
"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0
    )

    sql = (
        response
        .choices[0]
        .message
        .content
        .strip()
    )

    sql = (
        sql
        .replace("```sql", "")
        .replace("```", "")
        .strip()
    )

    return sql


# ========== 执行 SQL ==========
def run_sql(
    sql,
    db_path
):

    is_safe, message = validate_sql(sql)

    if not is_safe:
        return (
            None,
            f"安全检查未通过：{message}"
        )

    conn = sqlite3.connect(db_path)

    conn.set_authorizer(
        sqlite_read_only_authorizer
    )

    try:

        df = pd.read_sql_query(
            sql,
            conn
        )

        return df, None

    except Exception as e:

        return None, str(e)

    finally:

        conn.close()


# ========== SQL 自动修复 ==========
def fix_sql(
    question,
    sql,
    error,
    schema
):

    prompt = f"""
你是一个 SQLite SQL 专家。

下面的 SQL 执行失败，请修复它。

数据库结构：

{schema}

用户问题：

{question}

错误 SQL：

{sql}

错误信息：

{error}

安全要求：

1. 只能返回 SELECT 查询语句。
2. 如果使用 CTE，只能使用 WITH ... SELECT。
3. 禁止 INSERT、UPDATE、DELETE、DROP、
   CREATE、ALTER、REPLACE、ATTACH、DETACH、
   PRAGMA、VACUUM 等操作。
4. 不允许修改数据库。
5. 只返回修复后的 SQL。
6. 不要添加任何解释。

修复后的 SQL：
"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0
    )

    fixed_sql = (
        response
        .choices[0]
        .message
        .content
        .strip()
    )

    fixed_sql = (
        fixed_sql
        .replace("```sql", "")
        .replace("```", "")
        .strip()
    )

    return fixed_sql


# ============================================================
# DeepSeek 智能判断图表类型
# ============================================================
def choose_chart(
    question,
    df
):

    if df is None or len(df) == 0:
        return {
            "chart_type": "none"
        }

    columns_info = []

    for col in df.columns:

        dtype = str(
            df[col].dtype
        )

        unique_count = int(
            df[col].nunique()
        )

        sample_values = (
            df[col]
            .dropna()
            .head(5)
            .tolist()
        )

        columns_info.append(
            {
                "name": str(col),
                "dtype": dtype,
                "unique_count": unique_count,
                "sample_values": sample_values
            }
        )

    sample_data = df.head(10).to_dict(
        orient="records"
    )

    prompt = f"""
你是一个数据可视化专家。

请根据：
1. 用户的问题
2. 查询结果的字段信息
3. 查询结果中的示例数据

判断最适合使用哪一种图表。

用户问题：

{question}

查询结果字段：

{json.dumps(
    columns_info,
    ensure_ascii=False,
    default=str
)}

查询结果示例：

{json.dumps(
    sample_data,
    ensure_ascii=False,
    default=str
)}

你只能从下面5种类型中选择一种：

bar
适合分类数据之间的数量、销售额、利润、价格等比较。

pie
适合表示各类别的占比、比例、构成。
类别数量最好不要太多。

line
适合真正存在时间顺序的数据，例如年份、月份、日期，
用于展示趋势和变化。

scatter
适合两个连续数值变量之间的关系。

none
如果查询结果不适合用图表展示，就选择 none。

选择原则：

- 如果用户明确询问占比、比例、构成，优先考虑 pie。
- 如果用户询问时间趋势，并且结果中确实存在合理的时间字段，选择 line。
- 如果是不同类别之间的数值比较，选择 bar。
- 如果是两个连续数值变量之间的关系，选择 scatter。
- 如果结果只是一个简单结论、单个数字或者不适合可视化，选择 none。
- 不要为了画图而强行选择图表。

请严格返回 JSON，不要添加任何解释。

JSON 格式：

{{
    "chart_type": "bar",
    "x": "横轴字段名",
    "y": "纵轴字段名",
    "title": "图表标题"
}}

如果不需要图表：

{{
    "chart_type": "none"
}}
"""

    try:

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0
        )

        content = (
            response
            .choices[0]
            .message
            .content
            .strip()
        )

        content = (
            content
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        result = json.loads(content)

        chart_type = result.get(
            "chart_type",
            "none"
        )

        allowed_types = {
            "bar",
            "pie",
            "line",
            "scatter",
            "none"
        }

        if chart_type not in allowed_types:
            return {
                "chart_type": "none"
            }

        return result

    except Exception:
        return {
            "chart_type": "none"
        }


# ============================================================
# 根据 DeepSeek 的判断生成图表
# ============================================================
def render_chart(
    df,
    chart_config
):

    if (
        df is None
        or len(df) == 0
        or not chart_config
    ):
        return

    chart_type = chart_config.get(
        "chart_type",
        "none"
    )

    if chart_type == "none":
        return

    x_col = chart_config.get(
        "x"
    )

    y_col = chart_config.get(
        "y"
    )

    title = chart_config.get(
        "title",
        "数据可视化"
    )

    if (
        x_col not in df.columns
        and chart_type != "none"
    ):

        return

    if (
        y_col not in df.columns
        and chart_type != "none"
    ):

        return

    try:

        if chart_type == "bar":

            fig = px.bar(
                df,
                x=x_col,
                y=y_col,
                title=title
            )

        elif chart_type == "pie":

            fig = px.pie(
                df,
                names=x_col,
                values=y_col,
                title=title
            )

        elif chart_type == "line":

            fig = px.line(
                df,
                x=x_col,
                y=y_col,
                markers=True,
                title=title
            )

        elif chart_type == "scatter":

            fig = px.scatter(
                df,
                x=x_col,
                y=y_col,
                title=title
            )

        else:

            return

        st.plotly_chart(
            fig,
            use_container_width=True
        )

    except Exception:
        return


# ========== 自然语言总结 ==========
def generate_summary(
    question,
    df
):

    if df is None or len(df) == 0:
        return "未查询到相关数据。"

    data_str = df.to_string(
        index=False,
        max_rows=10
    )

    prompt = f"""
用户问题：

{question}

查询结果：

{data_str}

请用一句简洁的中文总结这个查询结果。

要求：
1. 直接说结论
2. 不要重复用户问题
3. 不要解释 SQL
4. 不要废话
"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.3
    )

    return (
        response
        .choices[0]
        .message
        .content
        .strip()
    )


# ========== 导出查询历史 ==========
def export_history_excel(history):

    buf = io.BytesIO()

    with pd.ExcelWriter(
        buf,
        engine="openpyxl"
    ) as writer:

        summary_rows = []

        for i, record in enumerate(history):

            summary_rows.append(
                {
                    "序号": i + 1,
                    "问题": record["question"],
                    "生成的SQL": record["sql"],
                    "图表类型":
                        record.get(
                            "chart_config",
                            {}
                        ).get(
                            "chart_type",
                            "none"
                        ),
                    "是否自动修复":
                        "是"
                        if record.get("auto_fixed")
                        else "否",
                    "总结":
                        record.get(
                            "summary",
                            ""
                        ),
                    "是否出错":
                        "是"
                        if record["error"]
                        else "否"
                }
            )

        pd.DataFrame(
            summary_rows
        ).to_excel(
            writer,
            sheet_name="查询摘要",
            index=False
        )

        for i, record in enumerate(history):

            if (
                record["result"] is not None
                and not record["error"]
            ):

                sheet_name = (
                    f"查询{i + 1}"
                )[:31]

                record["result"].to_excel(
                    writer,
                    sheet_name=sheet_name,
                    index=False
                )

    buf.seek(0)

    return buf


# ========== 侧边栏 ==========
with st.sidebar:

    st.header("📂 上传数据")

    uploaded_files = st.file_uploader(
        "上传 CSV / Excel 文件（支持多个）",
        type=["csv", "xlsx", "xls"],
        accept_multiple_files=True
    )


    # ========== 构建数据库 ==========
    if uploaded_files:

        if st.button(
            "🔄 构建数据库"
        ):

            with st.spinner(
                "正在构建数据库..."
            ):

                tmp = tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=".db"
                )

                db_path = tmp.name

                tmp.close()

                conn = sqlite3.connect(
                    db_path
                )


                # ==================================================
                # 读取上传的数据文件
                # ==================================================

                for f in uploaded_files:

                    file_name = f.name

                    base_name = os.path.splitext(
                        file_name
                    )[0]

                    file_ext = os.path.splitext(
                        file_name
                    )[1].lower()


                    # ==================================================
                    # CSV
                    # ==================================================

                    if file_ext == ".csv":

                        table_name = sanitize_table_name(
                            base_name
                        )

                        df = pd.read_csv(f)

                        df.columns = [
                            sanitize_column_name(c)
                            for c in df.columns
                        ]

                        df.to_sql(
                            table_name,
                            conn,
                            if_exists="replace",
                            index=False
                        )

                        st.write(
                            f"✔ 已导入："
                            f"{table_name}"
                            f"（{len(df)} 条）"
                        )


                    # ==================================================
                    # Excel
                    # ==================================================

                    elif file_ext in [
                        ".xlsx",
                        ".xls"
                    ]:

                        excel_sheets = pd.read_excel(
                            f,
                            sheet_name=None
                        )

                        for sheet_name, df in (
                            excel_sheets.items()
                        ):

                            table_name = sanitize_table_name(
                                f"{base_name}_{sheet_name}"
                            )

                            df.columns = [
                                sanitize_column_name(c)
                                for c in df.columns
                            ]

                            df.to_sql(
                                table_name,
                                conn,
                                if_exists="replace",
                                index=False
                            )

                            st.write(
                                f"✔ 已导入："
                                f"{table_name}"
                                f"（{len(df)} 条）"
                            )


                conn.close()


                st.session_state.db_path = (
                    db_path
                )


                (
                    st.session_state.schema,
                    st.session_state.table_names,
                    st.session_state.overview_data
                ) = generate_schema(
                    db_path
                )


                # 构建新数据库后清空历史
                st.session_state.history = []


                # 生成推荐问题
                with st.spinner(
                    "正在生成示例问题..."
                ):

                    st.session_state.suggested_questions = (
                        generate_suggested_questions(
                            st.session_state.schema
                        )
                    )


                st.success(
                    "✅ 数据库构建完成！"
                )


    # ========== 已加载的表 ==========
    if st.session_state.table_names:

        st.divider()

        st.header("📋 已加载的表")

        for table_name in (
            st.session_state.table_names
        ):

            st.write(
                f"• {table_name}"
            )


    # ========== 导出 ==========
    st.divider()

    st.header("📥 导出")

    if st.session_state.history:

        excel_buf = (
            export_history_excel(
                st.session_state.history
            )
        )

        st.download_button(
            label="📊 导出查询历史为 Excel",
            data=excel_buf,
            file_name="查询历史.xlsx",
            mime=(
                "application/vnd.openxmlformats-"
                "officedocument.spreadsheetml.sheet"
            )
        )

    else:

        st.caption(
            "暂无查询记录"
        )


    # ========== 清空记录 ==========
    st.divider()

    if st.button(
        "🗑️ 清空记录"
    ):

        st.session_state.history = []

        st.rerun()


# ========== 主界面 ==========
if not st.session_state.db_path:

    st.info(
        "👈 请先在左侧上传 CSV / Excel 文件并点击构建数据库"
    )

else:

    tab1, tab2 = st.tabs(
        [
            "📊 数据概览",
            "💬 自然语言查询"
        ]
    )


    # ========================================================
    # 数据概览
    # ========================================================

    with tab1:

        st.markdown(
            "### 📋 数据表概览"
        )

        for table_name, info in (
            st.session_state
            .overview_data
            .items()
        ):

            with st.expander(
                f"📄 {table_name}"
                f"  （{info['rows']} 行 × "
                f"{info['cols']} 列）"
            ):

                col1, col2, col3 = st.columns(3)


                col1.metric(
                    "总行数",
                    info["rows"]
                )

                col2.metric(
                    "总列数",
                    info["cols"]
                )

                col3.metric(
                    "缺失值列数",
                    sum(
                        1
                        for v in info["missing"].values()
                        if v > 0
                    )
                )


                st.markdown(
                    "**列信息**"
                )


                dtype_df = pd.DataFrame(
                    {
                        "列名":
                            list(
                                info["dtypes"].keys()
                            ),

                        "数据类型":
                            list(
                                info["dtypes"].values()
                            ),

                        "缺失值数量":
                            [
                                info["missing"].get(
                                    c,
                                    0
                                )
                                for c in info["dtypes"].keys()
                            ]
                    }
                )


                st.dataframe(
                    dtype_df,
                    use_container_width=True
                )


                st.markdown(
                    "**前5行数据预览**"
                )


                st.dataframe(
                    info["df"].head(5),
                    use_container_width=True
                )


    # ========================================================
    # 自然语言查询
    # ========================================================

    with tab2:

        # ====================================================
        # 推荐问题点击
        # ====================================================

        if st.session_state.clicked_question:

            question = (
                st.session_state.clicked_question
            )

            st.session_state.clicked_question = None

        else:

            question = None


        # ====================================================
        # 推荐问题
        # ====================================================

        if st.session_state.suggested_questions:

            st.markdown(
                "**💡 推荐问题（点击直接提问）**"
            )

            cols = st.columns(
                len(
                    st.session_state
                    .suggested_questions
                )
            )

            for i, q in enumerate(
                st.session_state
                .suggested_questions
            ):

                if cols[i].button(
                    q,
                    key=f"sq_{i}"
                ):

                    st.session_state.clicked_question = q

                    st.rerun()

            st.divider()


        # ====================================================
        # 历史聊天记录
        # ====================================================

        for record in (
            st.session_state.history
        ):

            with st.chat_message(
                "user"
            ):

                st.write(
                    record["question"]
                )


            with st.chat_message(
                "assistant"
            ):

                st.code(
                    record["sql"],
                    language="sql"
                )


                if record.get(
                    "auto_fixed"
                ):

                    st.warning(
                        "⚠️ 首次生成的 SQL 出错，"
                        "已自动修复并重新查询"
                    )


                if record["error"]:

                    st.error(
                        f"查询出错："
                        f"{record['error']}"
                    )


                elif record["result"] is not None:

                    st.dataframe(
                        record["result"]
                    )


                    chart_config = record.get(
                        "chart_config",
                        {
                            "chart_type": "none"
                        }
                    )


                    render_chart(
                        record["result"],
                        chart_config
                    )


                    st.info(
                        f"💡 {record['summary']}"
                    )


        # ====================================================
        # 输入框放在最下面
        # ====================================================

        if question:

            process_question = True

        else:

            question = st.chat_input(
                "用中文提问，例如：销售额最高的品类是什么？"
            )

            process_question = bool(
                question
            )


        # ====================================================
        # 处理新的问题
        # ====================================================

        if process_question and question:

            with st.spinner(
                "正在生成 SQL..."
            ):

                sql = generate_sql(
                    question,
                    st.session_state.schema,
                    st.session_state.history
                )


            is_safe, security_message = (
                validate_sql(sql)
            )


            df = None

            auto_fixed = False

            chart_config = {
                "chart_type": "none"
            }


            if not is_safe:

                error = (
                    "SQL 安全检查未通过："
                    + security_message
                )

                summary = (
                    "生成的 SQL 未通过安全检查，"
                    "系统已拒绝执行。"
                )


            else:

                with st.spinner(
                    "正在查询数据库..."
                ):

                    df, error = run_sql(
                        sql,
                        st.session_state.db_path
                    )


                retry_count = 0


                while (
                    error
                    and retry_count < 2
                ):

                    with st.spinner(
                        f"SQL 出错，第"
                        f"{retry_count + 1}"
                        f"次自动修复..."
                    ):

                        fixed_sql = fix_sql(
                            question,
                            sql,
                            error,
                            st.session_state.schema
                        )


                        df, fixed_error = (
                            run_sql(
                                fixed_sql,
                                st.session_state.db_path
                            )
                        )


                        if not fixed_error:

                            sql = fixed_sql

                            error = None

                            auto_fixed = True

                            break

                        else:

                            error = fixed_error

                    retry_count += 1


                if error:

                    summary = (
                        "查询出错，请换一种问法试试。"
                    )


                else:

                    with st.spinner(
                        "正在分析适合的图表..."
                    ):

                        chart_config = (
                            choose_chart(
                                question,
                                df
                            )
                        )


                    with st.spinner(
                        "正在生成总结..."
                    ):

                        summary = (
                            generate_summary(
                                question,
                                df
                            )
                        )


            st.session_state.history.append(
                {
                    "question": question,
                    "sql": sql,
                    "result": df,
                    "error": error,
                    "summary": summary,
                    "auto_fixed": auto_fixed,
                    "security_failed": not is_safe,
                    "chart_config": chart_config
                }
            )


            st.rerun()