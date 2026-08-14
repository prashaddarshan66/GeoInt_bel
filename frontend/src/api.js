import axios from "axios";

const BASE = process.env.REACT_APP_BACKEND_URL;
export const API = `${BASE}/api/v1`;

const client = axios.create({ baseURL: API, timeout: 60000 });

export const api = {
  status: () => client.get("/status").then((r) => r.data),
  listAois: () => client.get("/aois").then((r) => r.data),
  createAoi: (payload) => client.post("/aois", payload).then((r) => r.data),
  deleteAoi: (id) => client.delete(`/aois/${id}`).then((r) => r.data),
  searchImagery: (payload) => client.post("/imagery/search", payload).then((r) => r.data),
  listObservations: (aoiId) => client.get(`/aois/${aoiId}/observations`).then((r) => r.data),
  runChangeDetection: (payload) => client.post("/change-detection", payload).then((r) => r.data),
  runTimeseries: (payload) => client.post("/change-detection/timeseries", payload).then((r) => r.data),
  getJob: (jobId) => client.get(`/jobs/${jobId}`).then((r) => r.data),
  listChanges: (aoiId, params = {}) =>
    client.get(`/aois/${aoiId}/changes`, { params }).then((r) => r.data),
  dashboard: (aoiId) => client.get(`/aois/${aoiId}/dashboard`).then((r) => r.data),
  timeline: (aoiId) => client.get(`/aois/${aoiId}/timeline`).then((r) => r.data),
};
