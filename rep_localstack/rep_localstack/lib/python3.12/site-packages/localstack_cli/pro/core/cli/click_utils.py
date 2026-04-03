from localstack_cli.cli import console


def print_table(column_headers: list[str], columns: list[list]) -> None:
    """
    Print a generic table to console
    :param column_headers: the header of the columns
    :param columns: the values in the columns
    """
    from rich.table import Table

    assert len(column_headers) == len(columns)

    grid = Table(show_header=True, header_style="bold")
    for column in column_headers:
        grid.add_column(column)

    for r in zip(*columns, strict=False):
        grid.add_row(*r)

    console.print(grid)
