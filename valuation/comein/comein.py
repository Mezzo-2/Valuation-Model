from __future__ import annotations

from types import TracebackType
from typing import Any

from valuation.comein.client import McpClient


class ComeinClient:
    """Comein 工具的薄封装。传输仍走 McpClient。"""

    def __init__(self, mcp: McpClient | None = None) -> None:
        self._mcp = mcp or McpClient.comein()
        self._owns = mcp is None

    def __enter__(self) -> ComeinClient:
        if self._owns:
            self._mcp.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns:
            self._mcp.__exit__(exc_type, exc, tb)

    def call(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self._mcp.call(name, arguments, **kwargs)

    def list_tools(self) -> list[dict[str, Any]]:
        return self._mcp.list_tools()

    def finance_search(
        self,
        querys: list[str],
        report_dates: list[dict[str, Any]],
        search_data: list[str],
        statement_type: list[str] | None = None,
    ) -> Any:
        args: dict[str, Any] = {
            "querys": querys,
            "reportDates": report_dates,
            "searchData": search_data,
        }
        if statement_type:
            args["statementType"] = statement_type
        return self.call("company_finance_search", args)

    def price_performance(self, queries: list[str], include: list[str] | None = None) -> Any:
        args: dict[str, Any] = {"queries": queries}
        if include:
            args["include"] = include
        return self.call("pricePerformance", args)

    def stock_details(self, queries: list[str], include: list[str] | None = None) -> Any:
        args: dict[str, Any] = {"queries": queries}
        if include:
            args["include"] = include
        return self.call("get_stock_details", args)

    def financial_snapshot(self, queries: list[str], period_type: list[str] | None = None) -> Any:
        args: dict[str, Any] = {"queries": queries}
        if period_type:
            args["period_type"] = period_type
        return self.call("get_financial_snapshot", args)

    def profit_forecast(self, queries: list[str], **kwargs: Any) -> Any:
        args: dict[str, Any] = {"queries": queries}
        args.update(kwargs)
        return self.call("getStockProfitForecast", args)

    def main_business_segments(
        self,
        company_infos: list[dict[str, Any]],
        report_dates: list[dict[str, Any]],
        search_data: list[str],
        item_classify: list[str],
    ) -> Any:
        return self.call(
            "get_main_business_segments",
            {
                "companyInfos": company_infos,
                "reportDates": report_dates,
                "searchData": search_data,
                "itemClassify": item_classify,
            },
        )

    def research_query(self, type: str, **kwargs: Any) -> Any:
        args: dict[str, Any] = {"type": type}
        args.update(kwargs)
        return self.call("research_query", args)

    def search_comein_resource(self, query: str, **kwargs: Any) -> Any:
        args: dict[str, Any] = {"query": query, "filterImage": kwargs.pop("filterImage", True)}
        args.update(kwargs)
        return self.call("searchComeinResource", args)

    def search_chart(self, query: str, **kwargs: Any) -> Any:
        args: dict[str, Any] = {"query": query}
        args.update(kwargs)
        return self.call("searchChart", args)

    def screener_stock(self, **kwargs: Any) -> Any:
        return self.call("screenerStock", kwargs)
