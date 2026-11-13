"""Codal monthly production/sales extraction, staging and idempotent transfer."""
from __future__ import annotations
from datetime import datetime
from .appconfig import get_connection_string
from .codal_resource_stage import _find_sheet,_norm,_number,_period,_unit_code,_jalali_to_gregorian,_publish_date
from .codal_products_stage import _ensure_document

def _sales_rows(report):
    rows=_find_sheet(report,"Table_1")
    if len(rows)<3: raise ValueError("Table_1 فاقد سطرهای سرستون/داده کافی است.")
    h0=" ".join(str(x or "") for x in rows[0])
    h1=" ".join(str(x or "") for x in rows[1])
    if "دوره یک ماهه منتهی" not in h0 and "دورهیکماهه" not in _norm(h0):
        raise ValueError("سرستون دوره یک‌ماهه در Table_1 پیدا نشد.")
    required=("تعداد تولید","تعداد فروش","نرخ فروش","مبلغ فروش")
    if not all(_norm(x) in _norm(h1) for x in required):
        raise ValueError("سرستون‌های تولید/فروش Table_1 با الگوی مورد انتظار مطابقت ندارند.")
    output=[]
    section=None
    fy,fm,_=_period(report.get("Title",""))
    for row_no,row in enumerate(rows[2:],start=3):
        cells=list(row or [])+[None]*26
        name=str(cells[0] or "").strip()
        n=_norm(name)
        if "فروشداخلی" in n: section="DOMESTIC"; continue
        if "فروشصادراتی" in n: section="EXPORT"; continue
        if not section or not name or "جمع" in n or n.startswith(("درآمد","برگشت","تخفیف","تخفيف")): continue
        # Table_1 has a fixed, header-validated current-month 4-column block.
        production=_number(cells[13]); sales_qty=_number(cells[14])
        sales_rate=_number(cells[15]); sales_amount=_number(cells[16])
        if all(x is None for x in (production,sales_qty,sales_rate,sales_amount)):
            continue
        output.append({"name":name,"row_no":row_no,"sales_type":section,"year":fy,"month":fm,
                       "unit":_unit_code(cells[1]),"production":production,"sales_qty":sales_qty,
                       "sales_rate":sales_rate,"sales_amount":sales_amount})
    if not output: raise ValueError("هیچ ردیف تولید/فروش ماه جاری از Table_1 استخراج نشد.")
    keys=set()
    for r in output:
        key=(_norm(r["name"]),r["sales_type"])
        if key in keys: raise ValueError(f"محصول «{r['name']}» در بخش {r['sales_type']} بیش از یک بار تکرار شده است.")
        keys.add(key)
    return output

def stage_sales_reports(reports):
    cs=get_connection_string()
    if not cs: raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    summaries,failed=[],[]
    with pyodbc.connect(cs,timeout=15) as conn:
        cur=conn.cursor()
        for table in ("dbo.Companies","dbo.Industries","dbo.DataSources","dbo.SourceDocuments","dbo.Products","dbo.ProductMappings",
                      "dbo.ProductionSales_Stage","dbo.ProductionSales"):
            cur.execute("SELECT OBJECT_ID(?, 'U')",table)
            if cur.fetchone()[0] is None: raise RuntimeError(f"جدول {table} در SQL Server وجود ندارد.")
        cur.execute("""SELECT pm.SourceProductName,pm.NormalizedSourceProductName,pm.ProductID,p.Unit
                       FROM dbo.ProductMappings pm LEFT JOIN dbo.Products p ON p.ProductID=pm.ProductID
                       WHERE pm.IsActive=1""")
        mappings={}
        for src,norm,pid,unit in cur.fetchall():
            for key in (src,norm):
                key=_norm(key)
                if key: mappings.setdefault(key,[]).append((int(pid),unit))
        cur.execute("SELECT ProductID,ProductName,Unit FROM dbo.Products WHERE IsActive=1")
        products={}
        for pid,name,unit in cur.fetchall(): products.setdefault(_norm(name),[]).append((int(pid),unit))
        for report in reports:
            symbol=str(report.get("Symbol") or "").strip()
            trace=str(report.get("TracingNo") or "").strip()
            try:
                if report.get("Kind")!="MONTHLY_ACTIVITY": raise ValueError("ثبت تولید/فروش فقط برای گزارش فعالیت ماهانه انجام می‌شود.")
                source_rows=_sales_rows(report)
                company_id,doc_id,source_id,fy,fm,period_end=_ensure_document(cur,report,[r["name"] for r in source_rows])
                staged=[]
                for r in source_rows:
                    key=_norm(r["name"]); candidates=mappings.get(key,[])
                    if not candidates: candidates=products.get(key,[])
                    unique={x[0]:x[1] for x in candidates}
                    problems=[]; product_id=None; product_unit=None
                    if len(unique)==1:
                        product_id,product_unit=next(iter(unique.items()))
                    elif len(unique)>1: problems.append("نگاشت نام محصول مبهم است")
                    else: problems.append("ProductMapping یا محصول مرجع پیدا نشد")
                    if not r["unit"]: problems.append("واحد محصول در گزارش خالی یا ناشناخته است")
                    elif product_unit and r["unit"].upper()!=str(product_unit).strip().upper():
                        problems.append(f"واحد گزارش ({r['unit']}) با واحد مرجع ({product_unit}) متفاوت است")
                    staged.append((doc_id,company_id,r["name"],product_id,r["sales_type"],r["year"],r["month"],
                                   r["unit"],r["production"],r["sales_qty"],r["sales_rate"],r["sales_amount"],
                                   "ERROR" if problems else "VALID","؛ ".join(problems) if problems else None))
                cur.execute("DELETE FROM dbo.ProductionSales_Stage WHERE SourceDocumentID=?",doc_id)
                cur.fast_executemany=True
                cur.executemany("""INSERT dbo.ProductionSales_Stage
                  (SourceDocumentID,CompanyID,ProductNameRaw,ProductID,SalesType,FiscalYear,FiscalMonth,Unit,
                   ProductionQty,SalesQty,SalesRate,SalesAmount,ValidationStatus,ValidationMessage)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",staged)
                conn.commit()
                valid=[x for x in staged if x[-2]=="VALID"]
                invalid=[x for x in staged if x[-2]!="VALID"]
                transferred=0
                if not invalid:
                    # Transfer only after the entire report validates; preserve prior adjustment fields.
                    pub=_publish_date(report,datetime(*_jalali_to_gregorian(fy,fm,period_end))).date()
                    try:
                        for x in valid:
                            _,co,name,pid,stype,year,month,unit,pqty,sqty,rate,amount,_,_=x
                            cur.execute("""SELECT 1 FROM dbo.ProductionSales
                                WHERE CompanyID=? AND ProductID=? AND FiscalYear=? AND FiscalMonth=? AND SalesType=?""",
                                co,pid,year,month,stype)
                            if cur.fetchone():
                                cur.execute("""UPDATE dbo.ProductionSales SET
                                  ProductionQty=?,SalesQty=?,SalesRate=?,SalesAmount=?,Unit=?,Currency='IRR',
                                  ReportDate=?,SourceID=?,SourceDocumentID=?
                                  WHERE CompanyID=? AND ProductID=? AND FiscalYear=? AND FiscalMonth=? AND SalesType=?""",
                                  pqty,sqty,rate,amount,unit,pub,source_id,doc_id,co,pid,year,month,stype)
                            else:
                                cur.execute("""INSERT dbo.ProductionSales
                                  (CompanyID,ProductID,FiscalYear,FiscalMonth,ProductionQty,SalesQty,SalesRate,
                                   SalesAmount,Unit,Currency,ReportDate,SourceID,SourceDocumentID,SalesType)
                                  VALUES (?,?,?,?,?,?,?,?,?,'IRR',?,?,?,?)""",
                                  co,pid,year,month,pqty,sqty,rate,amount,unit,pub,source_id,doc_id,stype)
                            transferred+=1
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                issues=[{"sheet":"Table_1","row_no":source_rows[i]["row_no"],
                         "name":x[2],"message":x[13]} for i,x in enumerate(staged) if x[-2]!="VALID"]
                summaries.append({"symbol":symbol,"title":report.get("Title",""),"tracing_no":trace,
                                  "source_document_id":doc_id,"rows":len(staged),"valid":len(valid),
                                  "invalid":len(invalid),"transferred":transferred,"issues":issues})
            except Exception as exc:
                conn.rollback()
                failed.append({"symbol":symbol or "نماد نامشخص","title":report.get("Title",""),
                               "tracing_no":trace,"stage":"SALES_STAGE_TRANSFER",
                               "error":f"{type(exc).__name__}: {str(exc)[:700]}"})
    return {"summaries":summaries,"failed":failed,
            "total_rows":sum(x["rows"] for x in summaries),
            "valid_rows":sum(x["valid"] for x in summaries),
            "invalid_rows":sum(x["invalid"] for x in summaries),
            "transferred_rows":sum(x["transferred"] for x in summaries)}
