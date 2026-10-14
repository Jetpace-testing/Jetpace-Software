import { useMemo, useState } from "react";

type IconName =
  | "activity" | "alarm" | "bell" | "building" | "chevron" | "clock"
  | "device" | "download" | "droplet" | "gauge" | "home" | "location"
  | "map" | "menu" | "plus" | "pressure" | "refresh" | "report"
  | "search" | "settings" | "shield" | "temperature" | "trend" | "users"
  | "wifi" | "x";

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, React.ReactNode> = {
    home: <><path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/></>,
    activity: <><path d="M3 12h4l2-7 4 14 2-7h6"/></>,
    device: <><rect x="5" y="2" width="14" height="20" rx="3"/><path d="M9 6h6M9 18h6"/></>,
    map: <><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3Z"/><path d="M9 3v15M15 6v15"/></>,
    trend: <><path d="M3 19V5M3 19h18"/><path d="m6 15 4-4 3 2 6-7"/></>,
    report: <><path d="M6 2h9l4 4v16H6Z"/><path d="M14 2v5h5M9 12h6M9 16h6"/></>,
    alarm: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></>,
    users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1 1.55V21h-4v-.08a1.7 1.7 0 0 0-1-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.55-1H3v-4h.08a1.7 1.7 0 0 0 1.55-1 1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.55V3h4v.08a1.7 1.7 0 0 0 1 1.55 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.55 1H21v4h-.08a1.7 1.7 0 0 0-1.52 1Z"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></>,
    refresh: <><path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M18.5 9A7 7 0 0 0 6.1 6.1L4 8M5.5 15A7 7 0 0 0 17.9 17.9L20 16"/></>,
    menu: <><path d="M4 6h16M4 12h16M4 18h16"/></>,
    chevron: <path d="m9 18 6-6-6-6"/>,
    plus: <path d="M12 5v14M5 12h14"/>,
    temperature: <><path d="M14 14.76V5a4 4 0 0 0-8 0v9.76a6 6 0 1 0 8 0Z"/><path d="M10 9v8"/></>,
    droplet: <path d="M12 2S5 10 5 15a7 7 0 0 0 14 0c0-5-7-13-7-13Z"/>,
    pressure: <><circle cx="12" cy="12" r="9"/><path d="m12 12 4-4M7 16h10"/></>,
    wifi: <><path d="M5 12.55a11 11 0 0 1 14 0M8.5 16a6 6 0 0 1 7 0M12 20h.01"/></>,
    gauge: <><path d="M20 13a8 8 0 1 0-16 0"/><path d="m12 13 4-4M5 19h14"/></>,
    shield: <><path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    location: <><path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/></>,
    building: <><path d="M4 22V3h12v19M16 9h4v13M8 7h4M8 11h4M8 15h4M2 22h20"/></>,
    download: <><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M5 20h14"/></>,
    x: <path d="m6 6 12 12M18 6 6 18"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

const navItems: { label: string; icon: IconName }[] = [
  { label: "Home", icon: "home" }, { label: "Live Monitoring", icon: "activity" },
  { label: "Devices", icon: "device" }, { label: "Map & Locations", icon: "map" },
  { label: "Trends & Analysis", icon: "trend" }, { label: "Reports", icon: "report" },
  { label: "Alarms", icon: "alarm" }, { label: "User Management", icon: "users" },
  { label: "Settings", icon: "settings" },
];

const devices = [
  { id: "TMP-001", name: "AHU Supply Sensor", location: "AHU Room", temp: "22.1", humidity: "42.4", pressure: "13.2", status: "Online", alarm: "Normal", updated: "12 sec ago" },
  { id: "TMP-002", name: "Cleanroom 1 Sensor", location: "Cleanroom 1", temp: "18.2", humidity: "45.0", pressure: "25.0", status: "Online", alarm: "Warning", updated: "8 sec ago" },
  { id: "TMP-003", name: "Warehouse North", location: "Warehouse A", temp: "31.8", humidity: "62.7", pressure: "8.6", status: "Online", alarm: "Critical", updated: "16 sec ago" },
  { id: "TMP-004", name: "Electrical Panel", location: "Electrical Room", temp: "26.4", humidity: "38.1", pressure: "11.9", status: "Online", alarm: "Normal", updated: "24 sec ago" },
  { id: "TMP-005", name: "Utility Main Sensor", location: "Utility Area", temp: "—", humidity: "—", pressure: "—", status: "Offline", alarm: "Unknown", updated: "18 min ago" },
  { id: "TMP-006", name: "Production Line 2", location: "Production Area", temp: "23.7", humidity: "48.6", pressure: "12.4", status: "Online", alarm: "Normal", updated: "4 sec ago" },
  { id: "TMP-007", name: "Cleanroom 2 Sensor", location: "Cleanroom 2", temp: "21.8", humidity: "44.2", pressure: "14.1", status: "Online", alarm: "Normal", updated: "19 sec ago" },
];

function StatusBadge({ value }: { value: string }) {
  const kind = ["Critical", "Alarm"].includes(value) ? "critical" : value === "Warning" ? "warning" : ["Offline", "Unknown"].includes(value) ? "offline" : "normal";
  return <span className={`status-badge ${kind}`}><span className="status-dot"/>{value}</span>;
}

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{children}</section>;
}

function Select({ children, label }: { children: React.ReactNode; label: string }) {
  return <label className="select-wrap"><span className="sr-only">{label}</span><select aria-label={label}>{children}</select><Icon name="chevron" size={13}/></label>;
}

const kpis: { label: string; value: string; detail: string; icon: IconName; tone: string }[] = [
  { label: "Total Devices", value: "248", detail: "12 facilities", icon: "device", tone: "blue" },
  { label: "Online Devices", value: "231", detail: "93.1% connected", icon: "wifi", tone: "green" },
  { label: "Offline Devices", value: "17", detail: "2 newly offline", icon: "activity", tone: "gray" },
  { label: "Active Alarms", value: "5", detail: "2 critical · 3 warning", icon: "alarm", tone: "red" },
  { label: "Avg. Temperature", value: "22.4 °C", detail: "Within range", icon: "temperature", tone: "blue" },
  { label: "Avg. Humidity", value: "45.2%", detail: "Optimal level", icon: "droplet", tone: "teal" },
  { label: "Avg. Diff. Pressure", value: "12.8 Pa", detail: "Target 10–20 Pa", icon: "pressure", tone: "amber" },
];

function Sparkline({ color }: { color: string }) {
  return <svg className="sparkline" viewBox="0 0 100 28" preserveAspectRatio="none"><path d="M1 19C9 20 11 11 20 13s11 8 19 4 11-11 18-7 11 13 18 8 12-14 24-10" fill="none" stroke={color} strokeWidth="2.2"/></svg>;
}

function TrendChart() {
  return <div className="chart-area">
    <div className="y-labels"><span>30°</span><span>25°</span><span>20°</span><span>15°</span></div>
    <svg viewBox="0 0 760 220" preserveAspectRatio="none" role="img" aria-label="Temperature trend chart for the last 24 hours">
      <g className="grid"><path d="M0 20H760M0 73H760M0 126H760M0 179H760"/><path d="M0 20V190M126 20V190M253 20V190M380 20V190M506 20V190M633 20V190M760 20V190"/></g>
      <path className="threshold upper" d="M0 52H760"/><path className="threshold lower" d="M0 166H760"/>
      <path className="area-fill" d="M0 139C45 130 69 141 108 119S169 102 210 111s55-35 104-21 55 29 100 11 72-39 118-22 63 12 96-7 76-15 132-29V190H0Z"/>
      <path className="line-main" d="M0 139C45 130 69 141 108 119S169 102 210 111s55-35 104-21 55 29 100 11 72-39 118-22 63 12 96-7 76-15 132-29"/>
      <g className="points"><circle cx="210" cy="111" r="4"/><circle cx="414" cy="101" r="4"/><circle cx="628" cy="72" r="4"/></g>
    </svg>
    <div className="x-labels"><span>00:00</span><span>04:00</span><span>08:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>Now</span></div>
  </div>;
}

function FacilityMap() {
  return <div className="facility-map">
    <div className="zone cleanroom"><span>Cleanroom</span><small>Zone A</small><i className="marker normal m1"/><i className="marker warning m2"/></div>
    <div className="zone production"><span>Production</span><small>Lines 1–4</small><i className="marker normal m3"/><i className="marker normal m4"/><i className="marker critical m5"/></div>
    <div className="zone warehouse"><span>Warehouse</span><small>Storage A</small><i className="marker critical m6"/><i className="marker normal m7"/></div>
    <div className="zone utility"><span>Utility</span><small>Mechanical</small><i className="marker offline m8"/></div>
    <div className="zone electrical"><span>Electrical</span><small>Main room</small><i className="marker normal m9"/></div>
  </div>;
}

export default function App() {
  const [activeNav, setActiveNav] = useState("Home");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("All statuses");
  const [trend, setTrend] = useState("Temperature");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showAdd, setShowAdd] = useState(false);

  const filtered = useMemo(() => devices.filter(d =>
    (`${d.id} ${d.name} ${d.location}`).toLowerCase().includes(query.toLowerCase()) &&
    (status === "All statuses" || d.status === status)
  ), [query, status]);

  return <div className="app-shell">
    {sidebarOpen && <div className="mobile-overlay" onClick={() => setSidebarOpen(false)}/>}
    <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
      <div className="brand"><div className="brand-mark"><span/><span/><span/><span/></div><div><strong>JETPACE</strong><small>TEST AND MEASUREMENT INC</small></div></div>
      <nav aria-label="Main navigation">
        <p className="nav-label">Workspace</p>
        {navItems.map(item => <button key={item.label} className={`nav-item ${activeNav === item.label ? "active" : ""}`} onClick={() => { setActiveNav(item.label); setSidebarOpen(false); }}>
          <Icon name={item.icon}/><span>{item.label}</span>{item.label === "Alarms" && <b className="alarm-count">5</b>}
        </button>)}
      </nav>
      <div className="system-health"><div className="health-icon"><Icon name="shield" size={18}/></div><div><strong>System Health</strong><span><i/>All Systems Operational</span></div></div>
    </aside>

    <main>
      <header className="topbar">
        <button className="icon-button mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="Open navigation"><Icon name="menu"/></button>
        <div className="page-title"><span>Overview</span><strong>{activeNav === "Home" ? "Dashboard" : activeNav}</strong></div>
        <label className="global-search"><Icon name="search" size={17}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search device ID, location or parameter…"/></label>
        <div className="header-date"><Icon name="clock" size={16}/><div><span>Monday, 16 June</span><strong>10:32:45 AM</strong></div></div>
        <button className="icon-button" aria-label="Refresh data"><Icon name="refresh"/></button>
        <button className="icon-button notification" aria-label="Notifications"><Icon name="bell"/><span>5</span></button>
        <button className="profile"><div className="avatar">AM</div><div><strong>Alex Morgan</strong><span>Administrator</span></div><Icon name="chevron" size={14}/></button>
      </header>

      <div className="content">
        <div className="welcome-row"><div><h1>Operations overview</h1><p>Real-time environmental monitoring across all facilities.</p></div><div className="live-state"><i/>Live data <span>Updated 10 sec ago</span></div></div>

        <section className="kpi-grid" aria-label="Key performance indicators">
          {kpis.map(kpi => <article className="kpi-card" key={kpi.label}>
            <div className={`kpi-icon ${kpi.tone}`}><Icon name={kpi.icon}/></div>
            <div className="kpi-copy"><span>{kpi.label}</span><strong>{kpi.value}</strong><small className={kpi.tone === "red" ? "danger-text" : ""}>{kpi.detail}</small></div>
          </article>)}
        </section>

        <div className="dashboard-grid">
          <Panel className="device-panel">
            <div className="panel-heading">
              <div><h2>Live Device Monitoring</h2><p>Real-time sensor readings and connectivity</p></div>
              <button className="primary-button" onClick={() => setShowAdd(true)}><Icon name="plus" size={17}/>Add Device</button>
            </div>
            <div className="table-toolbar">
              <label className="table-search"><Icon name="search" size={16}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search devices…"/></label>
              <Select label="Area filter"><option>All areas</option><option>Cleanroom</option><option>Production</option><option>Warehouse</option></Select>
              <label className="select-wrap"><select aria-label="Status filter" value={status} onChange={e => setStatus(e.target.value)}><option>All statuses</option><option>Online</option><option>Offline</option></select><Icon name="chevron" size={13}/></label>
              <Select label="Alarm filter"><option>All alarms</option><option>Normal</option><option>Warning</option><option>Critical</option></Select>
            </div>
            <div className="table-scroll">
              <table>
                <thead><tr><th>Device</th><th>Location</th><th>Temp.</th><th>Humidity</th><th>Diff. Pressure</th><th>Connectivity</th><th>Alarm</th><th>Last Update</th></tr></thead>
                <tbody>{filtered.map(d => <tr key={d.id} className={d.id === "TMP-002" ? "selected-row" : ""}>
                  <td><button className="device-link"><span className="device-mini"><Icon name="device" size={15}/></span><span><strong>{d.id}</strong><small>{d.name}</small></span></button></td>
                  <td><span className="location-cell"><Icon name="location" size={14}/>{d.location}</span></td>
                  <td className={Number(d.temp) > 30 ? "reading-alert" : ""}>{d.temp}{d.temp !== "—" && " °C"}</td>
                  <td className={Number(d.humidity) > 60 ? "reading-alert" : ""}>{d.humidity}{d.humidity !== "—" && "%"}</td>
                  <td className={Number(d.pressure) > 20 ? "reading-warn" : ""}>{d.pressure}{d.pressure !== "—" && " Pa"}</td>
                  <td><StatusBadge value={d.status}/></td><td><StatusBadge value={d.alarm}/></td><td className="updated">{d.updated}</td>
                </tr>)}</tbody>
              </table>
              {filtered.length === 0 && <div className="empty-state"><Icon name="search" size={24}/><strong>No devices found</strong><span>Try changing your search or filters.</span></div>}
            </div>
            <div className="pagination"><span>Showing 1–{filtered.length} of 248 devices</span><div><button disabled>Previous</button><button className="page-current">1</button><button>2</button><button>3</button><button>Next</button></div></div>
          </Panel>

          <div className="side-stack">
            <Panel>
              <div className="panel-heading compact"><div><h2>Facility Overview</h2><p>Building 1 · Ground floor</p></div><Select label="Select floor"><option>Ground Floor</option><option>First Floor</option></Select></div>
              <FacilityMap/>
              <div className="map-legend"><span><i className="normal"/>Normal <b>202</b></span><span><i className="warning"/>Warning <b>24</b></span><span><i className="critical"/>Alarm <b>5</b></span><span><i className="offline"/>Offline <b>17</b></span></div>
            </Panel>

            <Panel className="selected-device">
              <div className="panel-heading compact"><div><span className="eyebrow">Selected device</span><h2>TMP-002 · Cleanroom 1 Sensor</h2></div><button className="icon-button small"><Icon name="chevron" size={15}/></button></div>
              <div className="device-meta"><span><Icon name="location" size={14}/>Building 1 / Cleanroom 1</span><span><Icon name="clock" size={14}/>16 Jun, 10:32:37</span></div>
              <div className="device-states"><StatusBadge value="Online"/><StatusBadge value="Warning"/></div>
              <div className="parameter-grid">
                <div className="parameter-card"><div><span className="parameter-icon blue"><Icon name="temperature" size={16}/></span><b>Temperature</b></div><strong>18.2 <small>°C</small></strong><Sparkline color="#168AAD"/><p>Range <b>18–24 °C</b></p></div>
                <div className="parameter-card"><div><span className="parameter-icon teal"><Icon name="droplet" size={16}/></span><b>Humidity</b></div><strong>45 <small>%</small></strong><Sparkline color="#16A36A"/><p>Range <b>35–55%</b></p></div>
                <div className="parameter-card warning-card"><div><span className="parameter-icon amber"><Icon name="pressure" size={16}/></span><b>Diff. Pressure</b></div><strong>25 <small>Pa</small></strong><Sparkline color="#E9A23B"/><p>Range <b>10–20 Pa</b></p></div>
              </div>
            </Panel>
          </div>
        </div>

        <div className="analysis-grid">
          <Panel className="trend-panel">
            <div className="panel-heading"><div><h2>Parameter Trends <span>(Last 24 Hours)</span></h2><p>TMP-002 · Cleanroom 1 Sensor</p></div><div className="heading-filters"><Select label="Select device"><option>TMP-002</option><option>TMP-001</option></Select><Select label="Time range"><option>Last 24 Hours</option><option>Last Hour</option><option>Last 7 Days</option><option>Custom Range</option></Select></div></div>
            <div className="trend-tabs">{["Temperature", "Humidity", "Differential Pressure"].map(t => <button key={t} className={trend === t ? "active" : ""} onClick={() => setTrend(t)}>{t}</button>)}</div>
            <div className="chart-topline"><div><strong>{trend === "Temperature" ? "22.4 °C" : trend === "Humidity" ? "45.2%" : "12.8 Pa"}</strong><span>Average reading</span></div><div className="chart-legend"><span><i className="data"/>Actual</span><span><i className="upper"/>Upper limit</span><span><i className="lower"/>Lower limit</span></div></div>
            <TrendChart/>
          </Panel>

          <Panel className="distribution-panel">
            <div className="panel-heading compact"><div><h2>Device Distribution</h2><p>Connectivity and health</p></div><button className="text-button">View details</button></div>
            <div className="donut-wrap"><div className="donut"><div><strong>248</strong><span>Total devices</span></div></div>
              <div className="distribution-list">
                <div><span><i className="normal"/>Online</span><b>231 <small>93.1%</small></b></div>
                <div><span><i className="offline"/>Offline</span><b>17 <small>6.9%</small></b></div>
                <div className="separate"><span><i className="warning"/>With warnings</span><b>24</b></div>
                <div><span><i className="critical"/>With alarms</span><b>5</b></div>
              </div>
            </div>
            <p className="overlap-note">Health conditions may overlap with connectivity totals.</p>
          </Panel>
        </div>

        <div className="lower-grid">
          <Panel className="alarms-panel">
            <div className="panel-heading compact"><div><h2>Recent Alarms</h2><p>Latest exceptions requiring attention</p></div><button className="text-button">View all alarms <Icon name="chevron" size={13}/></button></div>
            <div className="alarm-table"><div className="alarm-head"><span>Time</span><span>Severity</span><span>Device & Location</span><span>Parameter</span><span>Current</span><span>Limit</span><span>Status</span></div>
              {[
                ["10:28:14", "Critical", "TMP-003|Warehouse A", "Temperature", "31.8 °C", "> 28 °C", "Active"],
                ["10:12:41", "Warning", "TMP-002|Cleanroom 1", "Diff. Pressure", "25.0 Pa", "10–20 Pa", "Active"],
                ["09:54:07", "Critical", "TMP-019|Production Area", "Humidity", "68.2%", "< 55%", "Acknowledged"],
                ["09:31:22", "Warning", "TMP-005|Utility Area", "Connectivity", "Offline", "> 5 min", "Acknowledged"],
              ].map((a, i) => <div className="alarm-row" key={a[0]}>
                <span>{a[0]}<small>16 Jun</small></span><span><StatusBadge value={a[1]}/></span><span><strong>{a[2].split("|")[0]}</strong><small>{a[2].split("|")[1]}</small></span><span>{a[3]}</span><span className={a[1] === "Critical" ? "reading-alert" : "reading-warn"}>{a[4]}</span><span>{a[5]}</span><span className={`alarm-state ${i > 1 ? "ack" : ""}`}>{a[6]}</span>
              </div>)}
            </div>
          </Panel>

          <Panel className="reports-panel">
            <div className="panel-heading compact"><div><h2>Reports</h2><p>Automated operational summaries</p></div><button className="text-button">View all</button></div>
            <div className="report-list">
              {[["Daily Operations Report", "Today, 06:00 AM"], ["Weekly Performance Report", "16 Jun 2025"], ["Monthly Compliance Report", "01 Jun 2025"]].map((r, i) =>
                <div className="report-row" key={r[0]}><span className="report-icon"><Icon name="report" size={17}/></span><div><strong>{r[0]}</strong><span>{r[1]} · {i === 0 ? "1.8" : i === 1 ? "4.2" : "8.6"} MB</span></div><button aria-label={`Download ${r[0]}`}><Icon name="download" size={17}/></button></div>)}
            </div>
          </Panel>
        </div>

        <section className="quick-section"><div className="section-title"><h2>Quick Actions</h2><p>Common operational tasks</p></div><div className="quick-grid">
          {[["View Trends", "Analyze sensor history", "trend"], ["Generate Report", "Create a custom report", "report"], ["Manage Devices", "Configure device fleet", "settings"], ["Acknowledge Alarms", "Review active alarms", "alarm"]].map(q =>
            <button className="quick-action" key={q[0]}><span><Icon name={q[2] as IconName}/></span><div><strong>{q[0]}</strong><small>{q[1]}</small></div><Icon name="chevron" size={15}/></button>)}
        </div></section>
        <footer><span>© 2025 SENSEPRO Industrial Monitoring</span><span>Data refreshes every 10 seconds · Last sync successful</span><span>System version 2.4.1</span></footer>
      </div>
    </main>

    {showAdd && <div className="modal-backdrop" onClick={() => setShowAdd(false)}><div className="modal" onClick={e => e.stopPropagation()}>
      <div className="modal-head"><div><span className="eyebrow">Device management</span><h2>Add a new device</h2></div><button className="icon-button" onClick={() => setShowAdd(false)}><Icon name="x"/></button></div>
      <p>Register a sensor with SENSEPRO. Connection can be configured after creation.</p>
      <label>Device ID<input placeholder="e.g. TMP-249"/></label><label>Device name<input placeholder="Enter a descriptive name"/></label><label>Location<select><option>Select a location</option><option>Cleanroom 1</option><option>Production Area</option><option>Warehouse A</option></select></label>
      <div className="modal-actions"><button onClick={() => setShowAdd(false)}>Cancel</button><button className="primary-button" onClick={() => setShowAdd(false)}>Add Device</button></div>
    </div></div>}
  </div>;
}
