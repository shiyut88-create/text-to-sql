import pandas as pd
import sqlite3
import os

# 连接数据库
conn = sqlite3.connect('ecommerce.db')

# 读取并导入每个 CSV
files = {
    'customers': 'Customers.csv',
    'orders': 'Orders.csv',
    'order_items': 'Order Items.csv',
    'products': 'Products.csv',
    'order_payments': 'Order Payments.csv',
    'sellers': 'Sellers.csv',
}

for table_name, filename in files.items():
    if os.path.exists(filename):
        df = pd.read_csv(filename)
        # 列名转小写+去空格，方便 SQL 查询
        df.columns = [c.lower().replace(' ', '_') for c in df.columns]
        df.to_sql(table_name, conn, if_exists='replace', index=False)
        print(f"✅ 导入 {table_name}，共 {len(df)} 条记录")
    else:
        print(f"❌ 找不到文件：{filename}")

conn.commit()
conn.close()
print("\n数据库初始化完成！")
