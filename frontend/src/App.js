import { useEffect, useState, useCallback, useRef } from "react";
import "@/App.css";
import { api, API } from "@/api";
import MapView from "@/MapView";
import { toast, Toaster } from "sonner";
import {
  Crosshair, Search, Trash2, PlayCircle, Radar, Sun,
  Activity, Target, X, ChevronsDown, ChevronsUp, Zap, AlertTriangle, FileDown,
} from "lucide-react";

const SATELLITE_DEMO = "https://images.unsplash.com/photo-1744968777188-3e1b2ef23339?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMjh8MHwxfHNlYXJjaHwyfHxkaWdpdGFsJTIwbWFwJTIwZGFyayUyMGdyZWVufGVufDB8fHx8MTc4NjY4NzUzNXww&ixlib=rb-4.1.0&q=85";

function formatArea(m2) {
  if (m2 == null) return "—";
  if (m2 >= 1e6) return `${(m2 / 1e6).toFixed(2)} km²`;
  if (m2 >= 1e4) return `${(m2 / 1e4).toFixed(2)} ha`;
  return `${m2.toFixed(0)} m²`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

function confidenceTier(c) {
  if (c >= 0.8) return "high";
  if (c >= 0.6) return "med";
  return "low";
}

function CornerFrame({ children, testId }) {
  return (
    <div className="corner-brackets p-2" data-testid={testId}>
      <div className="cb-b" />
      {children}
    </div>
  );
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [aois, setAois] = useState([]);
  const [selectedAoi, setSelectedAoi] = useState(null);
  const [drawEnabled, setDrawEnabled] = useState(false);
  const [pendingAoi, setPendingAoi] = useState(null);
  const [aoiName, setAoiName] = useState("");

  const [startDate, setStartDate] = useState("2024-01-01");
  const [endDate, setEndDate] = useState("2024-06-30");
  const [cloudMax, setCloudMax] = useState(20);
  const [providers, setProviders] = useState({ s2: true, s1: true });

  const [observations, setObservations] = useState([]);
  const [changes, setChanges] = useState(null); // FeatureCollection
  const [dashboard, setDashboard] = useState(null);

  const [beforeObs, setBeforeObs] = useState(null);
  const [afterObs, setAfterObs] = useState(null);

  const [activeJob, setActiveJob] = useState(null);
  const [selectedChange, setSelectedChange] = useState(null);
  const [minConfidence, setMinConfidence] = useState(0);
  const [timelineOpen, setTimelineOpen] = useState(true);
  const [focusBbox, setFocusBbox] = useState(null);
  const pollRef = useRef(null);

  // Initial load
  useEffect(() => {
    api.status().then(setStatus).catch(() => setStatus({ demo_mode: true }));
    refreshAois();
  }, []);

  const refreshAois = async () => {
    try {
      const list = await api.listAois();
      setAois(list);
    } catch (e) {
      console.error(e);
    }
  };

  const refreshAoiData = useCallback(async (aoi) => {
    if (!aoi) return;
    try {
      const [obs, ch, dash] = await Promise.all([
        api.listObservations(aoi.id),
        api.listChanges(aoi.id, { min_confidence: minConfidence }),
        api.dashboard(aoi.id),
      ]);
      setObservations(obs);
      setChanges(ch);
      setDashboard(dash);
    } catch (e) {
      console.error(e);
    }
  }, [minConfidence]);

  useEffect(() => {
    if (selectedAoi) {
      refreshAoiData(selectedAoi);
      setFocusBbox(selectedAoi.bbox);
    } else {
      setObservations([]);
      setChanges(null);
      setDashboard(null);
    }
  }, [selectedAoi, refreshAoiData]);

  // Job polling
  useEffect(() => {
    if (!activeJob || activeJob.status === "COMPLETED" || activeJob.status === "FAILED") {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    pollRef.current = setInterval(async () => {
      try {
        const j = await api.getJob(activeJob.job_id);
        setActiveJob(j);
        if (j.status === "COMPLETED") {
          toast.success(`Change detection complete — ${j.output?.change_event_count ?? 0} events`);
          refreshAoiData(selectedAoi);
        }
        if (j.status === "FAILED") toast.error(`Job failed: ${j.error_message}`);
      } catch (e) {
        console.error(e);
      }
    }, 800);
    return () => clearInterval(pollRef.current);
  }, [activeJob, selectedAoi, refreshAoiData]);

  const handleAoiDrawn = (payload) => {
    setPendingAoi(payload);
    setDrawEnabled(false);
    setAoiName(`AOI-${new Date().toISOString().slice(0, 10)}-${Math.floor(Math.random() * 999)}`);
  };

  const commitAoi = async () => {
    if (!pendingAoi || !aoiName.trim()) {
      toast.error("Provide a name for the AOI");
      return;
    }
    try {
      const created = await api.createAoi({ name: aoiName, geometry: pendingAoi.geometry });
      toast.success(`AOI '${created.name}' saved`);
      setPendingAoi(null);
      setAoiName("");
      await refreshAois();
      setSelectedAoi(created);
    } catch (e) {
      toast.error(e.response?.data?.detail ?? "AOI save failed");
    }
  };

  const runSearch = async () => {
    if (!selectedAoi) return toast.error("Select an AOI first");
    const provs = [];
    if (providers.s2) provs.push("sentinel-2-l2a");
    if (providers.s1) provs.push("sentinel-1-grd");
    if (!provs.length) return toast.error("Select at least one provider");
    try {
      const res = await api.searchImagery({
        aoi_id: selectedAoi.id,
        start_date: startDate, end_date: endDate,
        max_cloud_cover: cloudMax, providers: provs,
      });
      toast.success(`${res.count} observations found${res.demo_mode ? " (DEMO)" : ""}`);
      await refreshAoiData(selectedAoi);
    } catch (e) {
      toast.error(e.response?.data?.detail ?? "Search failed");
    }
  };

  const runChangeDetection = async () => {
    if (!selectedAoi || !beforeObs || !afterObs) return toast.error("Select AOI + Before + After");
    if (beforeObs.observation_id === afterObs.observation_id)
      return toast.error("Before and After must differ");
    try {
      const job = await api.runChangeDetection({
        aoi_id: selectedAoi.id,
        before_observation_id: beforeObs.observation_id,
        after_observation_id: afterObs.observation_id,
        use_sar: providers.s1,
      });
      setActiveJob(job);
      toast.info("Change-detection job queued");
    } catch (e) {
      toast.error(e.response?.data?.detail ?? "Job submit failed");
    }
  };

  const deleteAoi = async (id) => {
    if (!window.confirm("Delete this AOI and all its data?")) return;
    await api.deleteAoi(id);
    if (selectedAoi?.id === id) setSelectedAoi(null);
    await refreshAois();
    toast.success("AOI deleted");
  };

  const filteredChanges = changes
    ? {
        ...changes,
        features: changes.features.filter(
          (f) => (f.properties.confidence ?? 0) >= minConfidence
        ),
      }
    : null;

  return (
    <div className="App">
      <Toaster position="top-right" theme="dark" />
      <MapView
        aois={aois}
        changes={filteredChanges}
        drawEnabled={drawEnabled}
        onAoiDrawn={handleAoiDrawn}
        onFeatureClick={setSelectedChange}
        focusBbox={focusBbox}
      />

      {/* Top bar */}
      <div className="absolute top-0 left-0 right-0 z-20 flex items-center justify-between px-3 py-2 bg-black/80 border-b" style={{ borderColor: "var(--border)" }}>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <Target size={16} className="text-[color:var(--accent)]" />
            <span className="font-head text-sm text-white">GEOINT</span>
            <span className="font-head text-xs text-[color:var(--text-secondary)]">CHANGE DETECTION</span>
          </div>
          {status?.demo_mode && (
            <span
              data-testid="demo-badge"
              className="font-head text-[10px] px-2 py-0.5 border"
              style={{ color: "var(--amber)", borderColor: "var(--amber)", letterSpacing: "0.18em" }}
            >
              ▲ DEMO DATA — CDSE CREDENTIALS NOT CONFIGURED
            </span>
          )}
        </div>
        <div className="flex items-center gap-4 font-mono text-[10px] text-[color:var(--text-secondary)]">
          <span>AOIs: <span className="text-white">{aois.length}</span></span>
          <span>MODE: <span className={status?.demo_mode ? "text-[color:var(--amber)]" : "text-[color:var(--success)]"}>{status?.demo_mode ? "DEMO" : "LIVE"}</span></span>
        </div>
      </div>

      {/* LEFT PANEL */}
      <div className="absolute left-3 top-14 bottom-3 w-[340px] panel flex flex-col z-10" style={{ maxHeight: "calc(100vh - 4rem)" }}>
        <div className="panel-header">
          <div className="font-head text-xs">MISSION CONTROL</div>
          <span className="label-mini">v1.0</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {/* AOI section */}
          <div className="p-3 divider">
            <div className="flex items-center justify-between mb-2">
              <span className="label-mini">AREAS OF INTEREST</span>
              <button
                data-testid="draw-aoi-btn"
                className={`tactical-btn ${drawEnabled ? "primary" : ""}`}
                onClick={() => { setDrawEnabled(!drawEnabled); setPendingAoi(null); }}
              >
                <Crosshair size={11} className="inline mr-1" /> {drawEnabled ? "Cancel" : "Draw"}
              </button>
            </div>
            {pendingAoi && (
              <div className="mb-2 p-2 border" style={{ borderColor: "var(--accent)" }}>
                <div className="label-mini mb-1">NEW AOI — {pendingAoi.area_km2.toFixed(3)} KM²</div>
                <input
                  data-testid="aoi-name-input"
                  className="tactical-input mb-2"
                  placeholder="AOI name"
                  value={aoiName}
                  onChange={(e) => setAoiName(e.target.value)}
                />
                <div className="flex gap-1">
                  <button data-testid="aoi-save-btn" className="tactical-btn primary flex-1" onClick={commitAoi}>Save AOI</button>
                  <button className="tactical-btn" onClick={() => setPendingAoi(null)}><X size={11} /></button>
                </div>
              </div>
            )}
            <div className="space-y-1" data-testid="aoi-list">
              {aois.length === 0 && (
                <div className="text-xs text-[color:var(--text-muted)] font-mono py-3 text-center scanlines">
                  NO AOIs — DRAW ONE ON THE MAP
                </div>
              )}
              {aois.map((a) => (
                <div
                  key={a.id}
                  data-testid={`aoi-row-${a.id}`}
                  onClick={() => setSelectedAoi(a)}
                  className={`p-2 flex items-center justify-between cursor-pointer border ${selectedAoi?.id === a.id ? "" : ""}`}
                  style={{
                    borderColor: selectedAoi?.id === a.id ? "var(--accent)" : "var(--border)",
                    background: selectedAoi?.id === a.id ? "var(--accent-muted)" : "transparent",
                  }}
                >
                  <div>
                    <div className="text-xs font-medium">{a.name}</div>
                    <div className="font-mono text-[10px] text-[color:var(--text-secondary)]">
                      {a.area_km2.toFixed(2)} km² · {fmtDate(a.created_at)}
                    </div>
                  </div>
                  <button
                    data-testid={`aoi-delete-${a.id}`}
                    className="tactical-btn danger"
                    onClick={(e) => { e.stopPropagation(); deleteAoi(a.id); }}
                  >
                    <Trash2 size={11} />
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Search form */}
          <div className="p-3 divider">
            <div className="label-mini mb-2">CATALOG SEARCH</div>
            <div className="space-y-2">
              <div>
                <div className="label-mini mb-1">START</div>
                <input data-testid="start-date" type="date" className="tactical-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
              </div>
              <div>
                <div className="label-mini mb-1">END</div>
                <input data-testid="end-date" type="date" className="tactical-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
              </div>
              <div>
                <div className="label-mini mb-1">MAX CLOUD % <span className="text-white font-mono">{cloudMax}</span></div>
                <input data-testid="cloud-slider" type="range" min="0" max="100" value={cloudMax} onChange={(e) => setCloudMax(+e.target.value)} className="w-full accent-cyan-400" />
              </div>
              <div className="flex gap-1">
                <button
                  data-testid="toggle-s2"
                  onClick={() => setProviders({ ...providers, s2: !providers.s2 })}
                  className={`tactical-btn flex-1 ${providers.s2 ? "primary" : ""}`}
                >
                  <Sun size={11} className="inline mr-1" /> S-2
                </button>
                <button
                  data-testid="toggle-s1"
                  onClick={() => setProviders({ ...providers, s1: !providers.s1 })}
                  className={`tactical-btn flex-1 ${providers.s1 ? "primary" : ""}`}
                >
                  <Radar size={11} className="inline mr-1" /> S-1
                </button>
              </div>
              <button
                data-testid="search-btn"
                className="tactical-btn primary w-full"
                disabled={!selectedAoi}
                onClick={runSearch}
              >
                <Search size={11} className="inline mr-1" /> Search Imagery
              </button>
            </div>
          </div>

          {/* Filter panel */}
          <div className="p-3 divider">
            <div className="label-mini mb-2">CHANGE FILTERS</div>
            <div>
              <div className="label-mini mb-1">MIN CONFIDENCE <span className="text-white font-mono">{(minConfidence * 100).toFixed(0)}%</span></div>
              <input
                data-testid="confidence-slider"
                type="range" min="0" max="100" value={minConfidence * 100}
                onChange={(e) => setMinConfidence(+e.target.value / 100)}
                className="w-full accent-cyan-400"
              />
            </div>
          </div>
        </div>
      </div>

      {/* RIGHT PANEL */}
      <div className="absolute right-3 top-14 bottom-3 w-[380px] panel flex flex-col z-10" style={{ maxHeight: "calc(100vh - 4rem)" }}>
        <div className="panel-header">
          <div className="font-head text-xs flex items-center gap-2"><Activity size={12} /> INTELLIGENCE FEED</div>
          {selectedAoi && (
            <span className="font-mono text-[10px] text-[color:var(--accent)]">{selectedAoi.name}</span>
          )}
        </div>

        <div className="flex-1 overflow-y-auto">
          {!selectedAoi && (
            <div className="p-6 text-center scanlines">
              <div className="label-mini mb-2">NO AOI SELECTED</div>
              <div className="text-xs text-[color:var(--text-muted)] font-mono">
                Select or draw an AOI to activate the intelligence feed.
              </div>
            </div>
          )}

          {selectedAoi && dashboard && (
            <div className="p-3 divider">
              <div className="label-mini mb-2">DASHBOARD</div>
              <div className="grid grid-cols-2 gap-1 font-mono text-[11px]">
                <StatCell label="AREA" value={`${dashboard.aoi.area_km2.toFixed(2)} km²`} />
                <StatCell label="OBS TOTAL" value={dashboard.observations.total} />
                <StatCell label="SENTINEL-2" value={dashboard.observations.sentinel2} />
                <StatCell label="SENTINEL-1" value={dashboard.observations.sentinel1} />
                <StatCell label="CHANGES" value={dashboard.changes.total} />
                <StatCell label="HIGH CONF" value={dashboard.changes.high_confidence} accent="amber" />
              </div>
              {dashboard.largest_change && (
                <div className="mt-2 p-2 border" style={{ borderColor: "var(--border)" }}>
                  <div className="label-mini mb-1">LARGEST CHANGE</div>
                  <div className="font-mono text-[11px]">
                    {dashboard.largest_change.change_type} · {formatArea(dashboard.largest_change.area_m2)}
                  </div>
                </div>
              )}
            </div>
          )}

          {activeJob && (
            <div className="p-3 divider">
              <div className="label-mini mb-2 flex items-center gap-2"><Zap size={11} /> JOB {activeJob.job_id.slice(0, 8)}</div>
              <div className="font-mono text-[11px] text-[color:var(--accent)] mb-1">[{activeJob.stage}]</div>
              <div className="h-1 bg-white/5">
                <div className="h-full transition-[width]" style={{ width: `${(activeJob.progress ?? 0) * 100}%`, background: activeJob.status === "FAILED" ? "var(--critical)" : "var(--accent)" }} />
              </div>
              <div className="mt-1 font-mono text-[10px] text-[color:var(--text-secondary)]">{activeJob.status}</div>
              {activeJob.error_message && (
                <div className="mt-1 flex gap-1 items-center text-[color:var(--critical)] text-[10px] font-mono">
                  <AlertTriangle size={10} /> {activeJob.error_message}
                </div>
              )}
            </div>
          )}

          {selectedAoi && observations.length > 0 && (
            <div className="p-3 divider">
              <div className="flex items-center justify-between mb-2">
                <div className="label-mini">OBSERVATIONS ({observations.length})</div>
              </div>
              <div className="grid grid-cols-2 gap-1 mb-2">
                <div className="label-mini">BEFORE</div>
                <div className="label-mini">AFTER</div>
              </div>
              <div className="space-y-1 max-h-52 overflow-y-auto" data-testid="observation-list">
                {observations.map((o) => (
                  <ObsRow
                    key={o.observation_id}
                    obs={o}
                    isBefore={beforeObs?.observation_id === o.observation_id}
                    isAfter={afterObs?.observation_id === o.observation_id}
                    onSetBefore={() => setBeforeObs(o)}
                    onSetAfter={() => setAfterObs(o)}
                  />
                ))}
              </div>
              <button
                data-testid="run-detection-btn"
                className="tactical-btn primary w-full mt-2"
                disabled={!beforeObs || !afterObs || activeJob?.status === "RUNNING"}
                onClick={runChangeDetection}
              >
                <PlayCircle size={11} className="inline mr-1" /> Run Change Detection
              </button>
            </div>
          )}

          {filteredChanges && filteredChanges.features.length > 0 && (
            <div className="p-3 divider">
              <div className="label-mini mb-2">CHANGE EVENTS ({filteredChanges.features.length})</div>
              <div className="space-y-1" data-testid="change-events-list">
                {filteredChanges.features.map((f) => (
                  <ChangeRow
                    key={f.properties.id}
                    change={f.properties}
                    selected={selectedChange?.id === f.properties.id}
                    onClick={() => setSelectedChange(f.properties)}
                  />
                ))}
              </div>
            </div>
          )}

          {selectedChange && (
            <ChangeDetail change={selectedChange} onClose={() => setSelectedChange(null)} />
          )}
        </div>
      </div>

      {/* BOTTOM TIMELINE */}
      {selectedAoi && (
        <div
          className="absolute bottom-3 z-10 panel"
          style={{ left: "calc(340px + 1.5rem)", right: "calc(380px + 1.5rem)" }}
        >
          <div className="panel-header">
            <div className="font-head text-xs">HISTORICAL TIMELINE</div>
            <button className="tactical-btn" onClick={() => setTimelineOpen(!timelineOpen)}>
              {timelineOpen ? <ChevronsDown size={12} /> : <ChevronsUp size={12} />}
            </button>
          </div>
          {timelineOpen && (
            <Timeline observations={observations} beforeObs={beforeObs} afterObs={afterObs}
                     onSetBefore={setBeforeObs} onSetAfter={setAfterObs} />
          )}
        </div>
      )}
    </div>
  );
}

function StatCell({ label, value, accent }) {
  return (
    <div className="p-2 border" style={{ borderColor: "var(--border)" }}>
      <div className="label-mini">{label}</div>
      <div className={`font-mono text-sm ${accent === "amber" ? "text-[color:var(--amber)]" : "text-white"}`}>{value}</div>
    </div>
  );
}

function ObsRow({ obs, isBefore, isAfter, onSetBefore, onSetAfter }) {
  const isS2 = obs.collection === "sentinel-2-l2a";
  const state = isBefore ? "BEFORE" : isAfter ? "AFTER" : null;
  return (
    <div
      className="flex items-center gap-2 p-1.5 border font-mono text-[10px]"
      style={{
        borderColor: state ? "var(--accent)" : "var(--border)",
        background: state ? "var(--accent-muted)" : "transparent",
      }}
    >
      <div className="flex-shrink-0">
        {isS2 ? <Sun size={11} className="text-yellow-400" /> : <Radar size={11} className="text-cyan-300" />}
      </div>
      <div className="flex-1 min-w-0">
        <div className="truncate text-white">{obs.product_id.slice(0, 24)}…</div>
        <div className="text-[color:var(--text-secondary)]">
          {fmtDate(obs.observation_datetime)}
          {isS2 && obs.cloud_percentage != null && ` · ${obs.cloud_percentage}%☁`}
        </div>
      </div>
      <button
        data-testid={`set-before-${obs.observation_id}`}
        className={`tactical-btn ${isBefore ? "primary" : ""}`}
        style={{ padding: "2px 6px", fontSize: 9 }}
        onClick={onSetBefore}
      >B</button>
      <button
        data-testid={`set-after-${obs.observation_id}`}
        className={`tactical-btn ${isAfter ? "primary" : ""}`}
        style={{ padding: "2px 6px", fontSize: 9 }}
        onClick={onSetAfter}
      >A</button>
    </div>
  );
}

function ChangeRow({ change, selected, onClick }) {
  const tier = confidenceTier(change.confidence);
  const barCls = tier === "high" ? "confidence-bar high" : "confidence-bar";
  return (
    <div
      data-testid={`change-row-${change.id}`}
      onClick={onClick}
      className="p-2 cursor-pointer"
      style={{
        borderTop: `1px solid ${selected ? "var(--accent)" : "var(--border)"}`,
        borderRight: `1px solid ${selected ? "var(--accent)" : "var(--border)"}`,
        borderBottom: `1px solid ${selected ? "var(--accent)" : "var(--border)"}`,
        borderLeft: `3px solid ${tier === "high" ? "var(--amber)" : "var(--accent)"}`,
        background: selected ? "var(--accent-muted)" : "transparent",
      }}
    >
      <div className="flex items-center justify-between">
        <div className="font-head text-[11px] text-white">{change.change_type}</div>
        <div className="font-mono text-[10px] text-[color:var(--text-secondary)]">
          {change.detected_by_sensors?.join("+").toUpperCase()}
        </div>
      </div>
      <div className="grid grid-cols-3 gap-1 mt-1 font-mono text-[10px]">
        <div><span className="text-[color:var(--text-secondary)]">AREA </span>{formatArea(change.area_m2)}</div>
        <div><span className="text-[color:var(--text-secondary)]">SCR </span>{change.change_score?.toFixed(2)}</div>
        <div><span className="text-[color:var(--text-secondary)]">CNF </span>{(change.confidence * 100).toFixed(0)}%</div>
      </div>
      <div className={barCls + " mt-1"}>
        <span style={{ width: `${change.confidence * 100}%` }} />
      </div>
    </div>
  );
}

function ChangeDetail({ change, onClose }) {
  return (
    <div className="p-3 divider" data-testid="change-detail">
      <div className="flex items-center justify-between mb-2">
        <div className="label-mini">EVENT DETAIL</div>
        <button className="tactical-btn" onClick={onClose}><X size={11} /></button>
      </div>
      <div className="font-mono text-[10px] text-[color:var(--text-secondary)] break-all mb-2">
        {change.id}
      </div>
      <div className="grid grid-cols-3 gap-1 mb-2">
        <CornerFrame testId="cf-before">
          <img src={SATELLITE_DEMO} alt="before" className="w-full aspect-square object-cover" />
          <div className="label-mini text-center mt-1">BEFORE</div>
        </CornerFrame>
        <CornerFrame testId="cf-after">
          <img src={SATELLITE_DEMO} alt="after" className="w-full aspect-square object-cover" style={{ filter: "hue-rotate(60deg) contrast(1.15)" }} />
          <div className="label-mini text-center mt-1">AFTER</div>
        </CornerFrame>
        <CornerFrame testId="cf-diff">
          <div className="w-full aspect-square" style={{ background: `linear-gradient(135deg, var(--amber), var(--critical))`, opacity: 0.8 }} />
          <div className="label-mini text-center mt-1">DIFF</div>
        </CornerFrame>
      </div>
      <div className="font-mono text-[11px] space-y-1">
        <Kv label="TYPE" value={change.change_type} />
        <Kv label="AREA" value={formatArea(change.area_m2)} />
        <Kv label="SCORE" value={change.change_score?.toFixed(3)} />
        <Kv label="CONFIDENCE" value={`${(change.confidence * 100).toFixed(1)}%`} accent />
        <Kv label="FIRST SEEN" value={fmtDate(change.first_seen)} />
        <Kv label="LAST SEEN" value={fmtDate(change.last_seen)} />
        <Kv label="SENSORS" value={change.detected_by_sensors?.join(" + ").toUpperCase()} />
      </div>
      {change.metrics && (
        <div className="mt-2 p-2 border" style={{ borderColor: "var(--border)" }}>
          <div className="label-mini mb-1">CONFIDENCE COMPONENTS</div>
          <div className="font-mono text-[10px] space-y-0.5">
            <Kv label="MAGNITUDE Z" value={change.metrics.magnitude_z?.toFixed(2)} />
            <Kv label="VALID PIXELS" value={`${(change.metrics.valid_pixel_fraction * 100).toFixed(0)}%`} />
            <Kv label="PERSISTENCE" value={change.metrics.persistence?.toFixed(2)} />
            <Kv label="MULTI-SENSOR" value={change.metrics.multi_sensor ? "YES" : "NO"} />
            {change.metrics.sar_iou > 0 && (
              <Kv label="SAR IoU" value={change.metrics.sar_iou?.toFixed(2)} />
            )}
          </div>
        </div>
      )}
      <a
        data-testid="export-report-btn"
        href={`${API}/changes/${change.id}/report`}
        target="_blank"
        rel="noopener noreferrer"
        className="tactical-btn primary w-full mt-3 inline-block text-center"
      >
        <FileDown size={11} className="inline mr-1" /> Export Briefing Card
      </a>
    </div>
  );
}

function Kv({ label, value, accent }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-[color:var(--text-secondary)]">{label}</span>
      <span className={accent ? "text-[color:var(--amber)]" : "text-white"}>{value}</span>
    </div>
  );
}

function Timeline({ observations, beforeObs, afterObs, onSetBefore, onSetAfter }) {
  if (!observations.length) {
    return (
      <div className="p-4 text-center scanlines">
        <div className="label-mini">NO OBSERVATIONS — RUN CATALOG SEARCH</div>
      </div>
    );
  }
  const dates = observations.map((o) => new Date(o.observation_datetime).getTime());
  const min = Math.min(...dates), max = Math.max(...dates);
  const span = Math.max(max - min, 1);
  return (
    <div className="p-3">
      <div className="relative h-14 border-l border-r" style={{ borderColor: "var(--border)" }}>
        <div className="absolute top-1/2 left-0 right-0 border-t" style={{ borderColor: "var(--border)" }} />
        {observations.map((o) => {
          const t = new Date(o.observation_datetime).getTime();
          const pct = ((t - min) / span) * 100;
          const isS2 = o.collection === "sentinel-2-l2a";
          const state = beforeObs?.observation_id === o.observation_id ? "BEFORE"
                      : afterObs?.observation_id === o.observation_id ? "AFTER" : null;
          return (
            <div
              key={o.observation_id}
              data-testid={`timeline-mark-${o.observation_id}`}
              className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 cursor-pointer group"
              style={{ left: `${pct}%` }}
              onClick={() => (beforeObs ? onSetAfter(o) : onSetBefore(o))}
              title={`${o.product_id} — ${fmtDate(o.observation_datetime)}`}
            >
              <div
                className="w-2 h-2 rotate-45"
                style={{
                  background: state ? "var(--accent)" : isS2 ? "#facc15" : "#22d3ee",
                  boxShadow: state ? "0 0 8px var(--accent)" : "none",
                }}
              />
              <div className="absolute -bottom-4 left-1/2 -translate-x-1/2 whitespace-nowrap font-mono text-[9px] opacity-0 group-hover:opacity-100 text-[color:var(--text-secondary)]">
                {fmtDate(o.observation_datetime)}
              </div>
            </div>
          );
        })}
      </div>
      <div className="flex justify-between font-mono text-[10px] mt-3 text-[color:var(--text-secondary)]">
        <span>{fmtDate(new Date(min).toISOString())}</span>
        <span className="text-[color:var(--text-muted)]">Click a marker to set BEFORE / AFTER</span>
        <span>{fmtDate(new Date(max).toISOString())}</span>
      </div>
    </div>
  );
}
