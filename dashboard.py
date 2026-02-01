# dashboard.py
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich import box

from bk5491b import func_name

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
                    mmeter_func_str: str = "",
                    mmeter_primary_str: str = "",
                    mmeter_secondary_str: str = "",
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
                    mrs_id: str = "",
                    mrs_out: str = "",
                    mrs_mode: str = "",
                    mrs_set: str = "",
                    mrs_in: str = "",
                    mrs_bo: str = "",
                    can_channel: str,
                    can_bitrate: int,
                    status_poll_period: float,
                    bus_load_pct=None,
                    bus_rx_fps=None,
                    bus_tx_fps=None,
                    watchdog=None):
    
    if not HAVE_RICH:
        mm = mmeter_primary_str or f"{meter_current_mA}mA"
        return f"E-Load V: {load_volts_mV}mV | AFG Freq: {afg_freq_read} | Meter: {mm}"

    layout = Layout()
    layout.split(
        Layout(name="top", size=12),
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

    try:
        f_i = int(getattr(hardware, 'mmeter_func', 0)) & 0xFF
        f2_i = int(getattr(hardware, 'mmeter_func2', f_i)) & 0xFF
        f2_en = bool(getattr(hardware, 'mmeter_func2_enabled', False))
        auto = bool(getattr(hardware, 'mmeter_autorange', True))
        rng_val = float(getattr(hardware, 'mmeter_range_value', 0.0) or 0.0)
        nplc = float(getattr(hardware, 'mmeter_nplc', 1.0) or 1.0)
        rel = bool(getattr(hardware, 'mmeter_rel_enabled', False))
        trig = int(getattr(hardware, 'mmeter_trig_source', 0)) & 0xFF
    except Exception:
        f_i, f2_i, f2_en, auto, rng_val, nplc, rel, trig = 0, 0, False, True, 0.0, 1.0, False, 0

    meter_table.add_row("Func", f"[yellow]{func_name(f_i)}[/]")
    meter_table.add_row("Auto", _badge(auto, 'ON', 'OFF'))
    meter_table.add_row("Range", f"[white]{'AUTO' if auto else (f'{rng_val:g}' if rng_val else '--')}[/]")
    meter_table.add_row("NPLC", f"[white]{nplc:g}[/]")
    meter_table.add_row("Rel", _badge(rel, 'ON', 'OFF'))

    trig_name = {0: 'IMM', 1: 'BUS', 2: 'MAN'}.get(trig, str(trig))
    meter_table.add_row("Trig", f"[white]{trig_name}[/]")

    meter_table.add_row("2nd", f"[white]{func_name(f2_i) if f2_en else 'OFF'}[/]")

    # MrSignal Panel
    mrs_table = Table.grid(padding=(0, 1))
    mrs_table.add_column(justify="right", style="bold white")
    mrs_table.add_column()
    if getattr(hardware, "mrsignal", None):
        mrs_table.add_row("ID", f"[white]{mrs_id or getattr(hardware, 'mrsignal_id', '—') or '—'}[/]")
        mrs_table.add_row("Output", _badge(str(mrs_out).strip().upper() in ['ON','1','TRUE'], 'ON', 'OFF'))
        mrs_table.add_row("Mode", f"[white]{mrs_mode or '—'}[/]")
        mrs_table.add_row("Set", f"[yellow]{mrs_set or '—'}[/]")
        mrs_table.add_row("Input", f"[cyan]{mrs_in or '—'}[/]")
        if mrs_bo:
            mrs_table.add_row("Float", f"[dim]{mrs_bo}[/]")
    else:
        mrs_table.add_row("Status", "[red]NOT DETECTED[/]")

    top_grid = Table.grid(expand=True)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_row(
        Panel(eload_table, title="[bold]E-Load[/]", border_style="cyan", box=box.ROUNDED),
        Panel(afg_table, title="[bold]AFG-2125[/]", border_style="green", box=box.ROUNDED),
        Panel(meter_table, title="[bold]Multimeter[/]", border_style="magenta", box=box.ROUNDED),
        Panel(mrs_table, title="[bold]MrSignal[/]", border_style="white", box=box.ROUNDED),
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
    if mmeter_primary_str:
        meas_meter.add_row("Function", f"[yellow]{mmeter_func_str or '--'}[/]")
        meas_meter.add_row("Primary", f"[yellow]{mmeter_primary_str}[/]")
        if mmeter_secondary_str:
            meas_meter.add_row("Secondary", f"[yellow]{mmeter_secondary_str}[/]")
    else:
        meas_meter.add_row("Current", f"[yellow]{meter_current_mA/1000:.3f} A[/]")

    # K1 relay status (drive + raw GPIO level when applicable)
    # We show only: (1) the logical drive state (ON/OFF) and (2) the raw GPIO level (HIGH/LOW).
    try:
        drive_on = bool(hardware.get_k1_drive())
    except Exception:
        drive_on = bool(getattr(hardware.relay, 'is_lit', False))

    try:
        pin_level = hardware.get_k1_pin_level()
    except Exception:
        pin_level = None

    backend = str(getattr(hardware, 'relay_backend', '') or '').strip() or 'unknown'

    drive_badge = Text.from_markup(_badge(drive_on, 'ON', 'OFF'))
    if pin_level is None:
        level_badge = Text.from_markup('[bold dim]--[/]')
    else:
        level_badge = Text.from_markup(_badge(bool(pin_level), 'HIGH', 'LOW'))

    gpio_panel = Panel(
        Align.center(
            Text.assemble(
                ("K1 Relay\n", "bold"),
                ("Backend: ", "bold"), (f"{backend}\n", "dim"),
                ("Drive: ", "bold"), drive_badge, ("\n", ""),
                ("GPIO:  ", "bold"), level_badge,
            ),
            vertical='middle',
        ),
        border_style='yellow',
        box=box.ROUNDED,
        title='[bold]K1[/]',
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
        (" Load: ", "bold"),
        ((f"{bus_load_pct:.1f}% " if isinstance(bus_load_pct, (int, float)) else "-- "), "yellow"),
        (" Poll: ", "bold"), (f"{status_poll_period:.2f}s ", "cyan"),
        (" AFG: ", "bold"), (f"{'Connected' if hardware.afg else 'Missing'}", "green" if hardware.afg else "red"),
        (" MR2: ", "bold"), (f"{'Connected' if getattr(hardware, 'mrsignal', None) else 'Missing'}", "green" if getattr(hardware, 'mrsignal', None) else "red"),
    )

    # Watchdog / control freshness (optional)
    if watchdog and isinstance(watchdog, dict):
        ages = watchdog.get("ages", {}) or {}
        timed_out = watchdog.get("timed_out", {}) or {}
        status_map = watchdog.get("states", {}) or {}

        def _seg(key: str, label: str):
            age = ages.get(key)
            st = status_map.get(key)
            to = bool(timed_out.get(key, False))
            if age is None:
                return (f" {label}:-- ", "dim")
            # Prefer the richer status (ok/warn/to) when available.
            if st == "warn":
                return (f" {label}:LAG({age:.1f}s) ", "yellow")
            if st == "to" or to:
                return (f" {label}:TO({age:.1f}s) ", "red")
            return (f" {label}:{age:.1f}s ", "green")

        status.append(" WD:", style="bold")
        status.append(*_seg("can", "CAN"))
        status.append(*_seg("k1", "K1"))
        status.append(*_seg("eload", "Load"))
        status.append(*_seg("afg", "AFG"))
        status.append(*_seg("mmeter", "DMM"))
        status.append(*_seg("mrsignal", "MR2"))

    layout["bottom"].update(Panel(status, box=box.SQUARE, border_style="blue"))
    return layout