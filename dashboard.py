# dashboard.py
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich import box

# Try to initialize Rich Console
try:
    console = Console(highlight=False)
    HAVE_RICH = True
except Exception:
    console = None
    HAVE_RICH = False

def _badge(ok: bool, true_label="ON", false_label="OFF"):
    """Helper to create color-coded text badges."""
    return f"[bold {'green' if ok else 'red'}]{true_label if ok else false_label}[/]"

def build_dashboard(hardware, *,
                    meter_current_mA: int,
                    load_volts_mV: int,
                    load_current_mA: int,
                    load_stat_func: str,
                    load_stat_curr: str,
                    load_stat_res: str,
                    load_stat_imp: str,
                    load_stat_short: str,
                    afg_freq_read: str,
                    afg_ampl_read: str,
                    afg_offset_read: str,
                    afg_duty_read: str,
                    afg_out_read: str,
                    afg_shape_read: str,
                    can_channel: str,
                    can_bitrate: int,
                    status_poll_period: float):
    
    if not HAVE_RICH:
        return f"E-Load V: {load_volts_mV}mV | AFG Freq: {afg_freq_read} | Meter I: {meter_current_mA}mA"

    layout = Layout()
    layout.split(
        Layout(name="top", size=11),
        Layout(name="middle", ratio=1),
        Layout(name="bottom", size=3),
    )

    # --- TOP: Instrument Status ---
    # E-LOAD Panel
    eload_table = Table.grid(padding=(0, 1))
    eload_table.add_column(justify="right", style="bold cyan")
    eload_table.add_column()
    if hardware.e_load:
        visa_id = hardware.e_load.resource_name
        eload_table.add_row("ID", f"[white]{visa_id}[/]")
        el_on = str(load_stat_imp or '').strip().upper() in ['ON', '1']
        eload_table.add_row("Enable", f"{_badge(el_on)}")
        eload_table.add_row("Mode", f"[white]{load_stat_func or ''}[/]")
        if load_stat_func and load_stat_func.strip().upper().startswith("CURR"):
            eload_table.add_row("Set (I)", f"[yellow]{load_stat_curr or ''}[/]")
        else:
            eload_table.add_row("Set", f"[yellow]{(load_stat_curr or load_stat_res or '').strip()}[/]")
    else:
        eload_table.add_row("Status", "[red]NOT DETECTED[/]")

    # AFG Panel
    afg_table = Table.grid(padding=(0, 1))
    afg_table.add_column(justify="right", style="bold green")
    afg_table.add_column()
    if hardware.afg:
        afg_table.add_row("ID", f"[white]{hardware.afg_id or 'Unknown'}[/]")
        
        is_on = str(afg_out_read).strip().upper() in ['ON', '1']
        afg_table.add_row("Output", _badge(is_on))
        
        afg_table.add_row("Freq", f"[yellow]{afg_freq_read} Hz[/]")
        afg_table.add_row("Ampl", f"[yellow]{afg_ampl_read} Vpp[/]")
        afg_table.add_row("Offset", f"[cyan]{afg_offset_read} V[/]")
        
        duty_style = "yellow" if "SQU" in str(afg_shape_read).upper() else "dim white"
        afg_table.add_row("Duty", f"[{duty_style}]{afg_duty_read} %[/]")
        
        afg_table.add_row("Shape", f"[white]{afg_shape_read}[/]")
    else:
        afg_table.add_row("Status", "[red]NOT DETECTED[/]")

    # Multimeter Panel
    meter_table = Table.grid(padding=(0, 1))
    meter_table.add_column(justify="right", style="bold magenta")
    meter_table.add_column()
    meter_table.add_row("ID", f"[white]{hardware.mmeter_id or '—'}[/]")
    meter_table.add_row("Range", f"[white]{hardware.multi_meter_range}[/]")

    top_grid = Table.grid(expand=True)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_row(
        Panel(eload_table, title="[bold]E-Load[/]", border_style="cyan", box=box.ROUNDED),
        Panel(afg_table, title="[bold]AFG-2125[/]", border_style="green", box=box.ROUNDED),
        Panel(meter_table, title="[bold]Multimeter[/]", border_style="magenta", box=box.ROUNDED),
    )
    layout["top"].update(top_grid)

    # --- MIDDLE: Measurements ---
    meas_eload = Table(title="[bold]E-Load Meas[/]", box=box.SIMPLE_HEAVY, expand=True)
    meas_eload.add_column("Metric", style="bold cyan", no_wrap=True)
    meas_eload.add_column("Value", justify="right")
    meas_eload.add_row("Voltage", f"[green]{load_volts_mV/1000:.3f} V[/]")
    meas_eload.add_row("Current", f"[green]{load_current_mA/1000:.3f} A[/]")

    meas_meter = Table(title="[bold]Meter Meas[/]", box=box.SIMPLE_HEAVY, expand=True)
    meas_meter.add_column("Metric", style="bold magenta", no_wrap=True)
    meas_meter.add_column("Value", justify="right")
    meas_meter.add_row("Current", f"[yellow]{meter_current_mA/1000:.3f} A[/]")

    # GPIO Status
    # `hardware.relay.is_lit` reflects the *logical* coil state (energized / de-energized).
    # `hardware.dut_power_on` reflects the intended DUT power state after applying wiring semantics (NO/NC).
    coil_badge = Text.from_markup(_badge(hardware.relay.is_lit, "ENERGIZED", "DE-ENERGIZED"))
    power_badge = Text.from_markup(_badge(getattr(hardware, "dut_power_on", False), "POWER ON", "POWER OFF"))

    gpio_panel = Panel(
        Align.center(
            Text.assemble(
                ("K1 Relay Coil\n", "bold"), coil_badge, "\n\n", ("DUT\n", "bold"), power_badge
            ),
            vertical="middle",
        ),
        border_style="yellow",
        box=box.ROUNDED,
        title="[bold]GPIO[/]",
    )

    mid = Table.grid(expand=True)
    mid.add_column(ratio=2)
    mid.add_column(ratio=2)
    mid.add_column(ratio=1)
    mid.add_row(meas_eload, meas_meter, gpio_panel)
    layout["middle"].update(mid)

    # --- BOTTOM: Status Bar ---
    status = Text.assemble(
        (" CAN: ", "bold"), (f"{can_channel}@{can_bitrate//1000}k ", "cyan"),
        (" Poll: ", "bold"), (f"{status_poll_period:.2f}s ", "cyan"),
        (" AFG: ", "bold"), (f"{'Connected' if hardware.afg else 'Missing'}", "green" if hardware.afg else "red")
    )
    layout["bottom"].update(Panel(status, box=box.SQUARE, border_style="blue"))
    return layout