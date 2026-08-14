import { useEffect, useRef } from "react";
import "ol/ol.css";
import { Map, View } from "ol";
import TileLayer from "ol/layer/Tile";
import VectorLayer from "ol/layer/Vector";
import ImageLayer from "ol/layer/Image";
import XYZ from "ol/source/XYZ";
import VectorSource from "ol/source/Vector";
import Static from "ol/source/ImageStatic";
import { Draw, Modify } from "ol/interaction";
import GeoJSON from "ol/format/GeoJSON";
import { Style, Stroke, Fill, Circle as CircleStyle } from "ol/style";
import { fromLonLat, toLonLat } from "ol/proj";
import { getArea } from "ol/sphere";

const styleAoi = new Style({
  stroke: new Stroke({ color: "#22d3ee", width: 2, lineDash: [6, 4] }),
  fill: new Fill({ color: "rgba(34, 211, 238, 0.08)" }),
});

const styleChangeByConfidence = (feature) => {
  const c = feature.get("confidence") ?? 0.5;
  const color = c >= 0.8 ? "#ffb020" : c >= 0.6 ? "#22d3ee" : "#94a3b8";
  const fill = c >= 0.8 ? "rgba(255,176,32,0.22)"
             : c >= 0.6 ? "rgba(34,211,238,0.18)"
             : "rgba(148,163,184,0.12)";
  return new Style({
    stroke: new Stroke({ color, width: 1.5 }),
    fill: new Fill({ color: fill }),
    image: new CircleStyle({ radius: 5, fill: new Fill({ color }) }),
  });
};

export default function MapView({
  onAoiDrawn,
  aois,
  changes,
  drawEnabled,
  onFeatureClick,
  focusBbox,
  imageryLayer, // { url, bbox: [minx,miny,maxx,maxy], opacity }
}) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const aoiSourceRef = useRef(new VectorSource());
  const drawSourceRef = useRef(new VectorSource());
  const changesSourceRef = useRef(new VectorSource());
  const drawInteractionRef = useRef(null);
  const imageryLayerRef = useRef(null);

  // Init map once
  useEffect(() => {
    if (mapRef.current) return;
    const base = new TileLayer({
      source: new XYZ({
        // CartoDB dark-matter tiles
        url: "https://{a-c}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        attributions: "© OpenStreetMap contributors © CARTO",
        crossOrigin: "anonymous",
      }),
    });
    const aoiLayer = new VectorLayer({ source: aoiSourceRef.current, style: styleAoi });
    const drawLayer = new VectorLayer({ source: drawSourceRef.current, style: styleAoi });
    const changeLayer = new VectorLayer({
      source: changesSourceRef.current,
      style: styleChangeByConfidence,
    });

    mapRef.current = new Map({
      target: containerRef.current,
      layers: [base, aoiLayer, changeLayer, drawLayer],
      view: new View({ center: fromLonLat([12.5, 41.9]), zoom: 5 }),
    });

    // Modify existing AOI features
    mapRef.current.addInteraction(new Modify({ source: aoiSourceRef.current }));

    // Click handler for change polygons
    mapRef.current.on("singleclick", (evt) => {
      let hit = null;
      mapRef.current.forEachFeatureAtPixel(evt.pixel, (feat, layer) => {
        if (layer === changeLayer && !hit) hit = feat;
      });
      if (hit && onFeatureClick) {
        onFeatureClick(hit.getProperties());
      }
    });
  }, [onFeatureClick]);

  // Toggle drawing
  useEffect(() => {
    const m = mapRef.current;
    if (!m) return;
    if (drawInteractionRef.current) {
      m.removeInteraction(drawInteractionRef.current);
      drawInteractionRef.current = null;
    }
    if (drawEnabled) {
      drawSourceRef.current.clear();
      const draw = new Draw({ source: drawSourceRef.current, type: "Polygon" });
      draw.on("drawend", (evt) => {
        const feature = evt.feature;
        const geom = feature.getGeometry();
        const areaM2 = getArea(geom);
        const coords3857 = geom.getCoordinates();
        const coords4326 = coords3857.map((ring) => ring.map((p) => toLonLat(p)));
        if (onAoiDrawn) {
          onAoiDrawn({
            geometry: { type: "Polygon", coordinates: coords4326 },
            area_km2: areaM2 / 1_000_000,
          });
        }
      });
      m.addInteraction(draw);
      drawInteractionRef.current = draw;
    }
    // eslint-disable-next-line
  }, [drawEnabled]);

  // Render AOIs
  useEffect(() => {
    aoiSourceRef.current.clear();
    const format = new GeoJSON();
    (aois || []).forEach((a) => {
      const feat = format.readFeature(
        { type: "Feature", geometry: a.geometry, properties: { id: a.id, name: a.name } },
        { dataProjection: "EPSG:4326", featureProjection: "EPSG:3857" }
      );
      aoiSourceRef.current.addFeature(feat);
    });
  }, [aois]);

  // Render change polygons
  useEffect(() => {
    changesSourceRef.current.clear();
    if (!changes || !changes.features) return;
    const format = new GeoJSON();
    changes.features.forEach((f) => {
      const feat = format.readFeature(f, {
        dataProjection: "EPSG:4326",
        featureProjection: "EPSG:3857",
      });
      // ensure properties are on the feature
      Object.entries(f.properties || {}).forEach(([k, v]) => feat.set(k, v));
      changesSourceRef.current.addFeature(feat);
    });
  }, [changes]);

  // Focus bbox
  useEffect(() => {
    if (!focusBbox || !mapRef.current) return;
    const [minx, miny, maxx, maxy] = focusBbox;
    const extent = [
      ...fromLonLat([minx, miny]),
      ...fromLonLat([maxx, maxy]),
    ];
    mapRef.current.getView().fit(extent, {
      padding: [80, 400, 140, 400],
      duration: 500,
      maxZoom: 15,
    });
  }, [focusBbox]);

  // Imagery layer (satellite preview PNG)
  useEffect(() => {
    const m = mapRef.current;
    if (!m) return;
    if (imageryLayerRef.current) {
      m.removeLayer(imageryLayerRef.current);
      imageryLayerRef.current = null;
    }
    if (imageryLayer && imageryLayer.url && imageryLayer.bbox) {
      const [minx, miny, maxx, maxy] = imageryLayer.bbox;
      const extent = [
        ...fromLonLat([minx, miny]),
        ...fromLonLat([maxx, maxy]),
      ];
      const layer = new ImageLayer({
        source: new Static({
          url: imageryLayer.url,
          imageExtent: extent,
          crossOrigin: "anonymous",
        }),
        opacity: imageryLayer.opacity ?? 0.85,
      });
      // Insert above base (index 0) but below vector layers
      m.getLayers().insertAt(1, layer);
      imageryLayerRef.current = layer;
    }
  }, [imageryLayer]);

  return (
    <div
      ref={containerRef}
      data-testid="ol-map"
      className={`absolute inset-0 ${drawEnabled ? "map-cursor-crosshair" : ""}`}
    />
  );
}
