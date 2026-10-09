import axios from "axios";

const BASE = process.env.REACT_APP_BACKEND_URL;
export const API_BASE = `${BASE}/api`;

export const api = axios.create({ baseURL: API_BASE });

api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem("examnest_token");
  if (token) cfg.headers.Authorization = `Bearer ${token}`;
  return cfg;
});

export const profileApi = {
  updateSettings: (data) => api.put("/auth/me/settings", data).then((r) => r.data),
};

export const authApi = {
  signup: (data) => api.post("/auth/signup", data).then((r) => r.data),
  login: (data) => api.post("/auth/login", data).then((r) => r.data),
  me: () => api.get("/auth/me").then((r) => r.data),
};

export const questionsApi = {
  list: (params) => api.get("/questions", { params }).then((r) => r.data),
  create: (data) => api.post("/questions", data).then((r) => r.data),
  update: (id, data) => api.put(`/questions/${id}`, data).then((r) => r.data),
  patch: (id, data) => api.patch(`/questions/${id}`, data).then((r) => r.data),
  remove: (id) => api.delete(`/questions/${id}`).then((r) => r.data),
  usage: (id) => api.get(`/questions/${id}/usage`).then((r) => r.data),
};

export const teacherApi = {
  myPerms: () => api.get("/teacher/permissions").then((r) => r.data),
  list: () => api.get("/teachers").then((r) => r.data),
  setPerms: (data) => api.post("/teachers/permissions", data).then((r) => r.data),
};

export const practiceApi = {
  generate: (data) => api.post("/practice/generate", data).then((r) => r.data),
};

export const importApi = {
  parse: (formData) => api.post("/import/parse", formData, {
    headers: { "Content-Type": "multipart/form-data" }
  }).then((r) => r.data),
  start: (formData) => api.post("/import/start", formData, {
    headers: { "Content-Type": "multipart/form-data" }
  }).then((r) => r.data),
  jobStatus: (jobId) => api.get(`/import/jobs/${jobId}`).then((r) => r.data),
  commit: (questions) => api.post("/import/commit", { questions }).then((r) => r.data),
  predictDifficulty: (texts) => api.post("/import/ai-difficulty", { texts }).then((r) => r.data),
};

export const testsApi = {
  list: () => api.get("/tests").then((r) => r.data),
  get: (id, includeQuestions = false) =>
    api.get(`/tests/${id}`, { params: { include_questions: includeQuestions } }).then((r) => r.data),
  create: (data) => api.post("/tests", data).then((r) => r.data),
  remove: (id) => api.delete(`/tests/${id}`).then((r) => r.data),
  random: (data) => api.post("/tests/random", data).then((r) => r.data),
  assign: (data) => api.post("/tests/assign", data).then((r) => r.data),
  parentAssign: (data) => api.post("/tests/parent-assign", data).then((r) => r.data),
  questionAnalysis: (id) => api.get(`/tests/${id}/question-analysis`).then((r) => r.data),
};

export const attemptsApi = {
  start: (test_id, use_retake_pass = false) => api.post("/attempts/start", { test_id, use_retake_pass }).then((r) => r.data),
  submit: (data) => api.post("/attempts/submit", data).then((r) => r.data),
  list: (params) => api.get("/attempts", { params }).then((r) => r.data),
  get: (id) => api.get(`/attempts/${id}`).then((r) => r.data),
  hint: (attemptId, question_id) => api.post(`/attempts/${attemptId}/hint`, { question_id }).then((r) => r.data),
};

export const analyticsApi = {
  admin: () => api.get("/analytics/admin").then((r) => r.data),
  student: (sid) => api.get(`/analytics/student/${sid}`).then((r) => r.data),
  parent: (cid) => api.get(`/analytics/parent/${cid}`).then((r) => r.data),
  leaderboard: (kind = "coins") => api.get("/leaderboard", { params: { kind } }).then((r) => r.data),
  liveLeaderboard: (testId) => api.get("/leaderboard/live", { params: testId ? { test_id: testId } : {} }).then((r) => r.data),
  questionHeatmap: (subject) => api.get("/analytics/question-heatmap", { params: subject ? { subject } : {} }).then((r) => r.data),
};

export const errorsApi = {
  list: (limit = 100) => api.get("/admin/errors", { params: { limit } }).then((r) => r.data),
  clear: () => api.post("/admin/errors/clear").then((r) => r.data),
};

export const aiTutorApi = {
  solve: (data) => api.post("/ai/doubt-solve", data).then((r) => r.data),
  history: () => api.get("/ai/doubt-history").then((r) => r.data),
};

export const notesQuizApi = {
  fromDocument: (formData) => api.post("/practice/from-document", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  }).then((r) => r.data),
  myQuizzes: () => api.get("/practice/my-quizzes").then((r) => r.data),
};

export const coachApi = {
  plan: () => api.get("/coach/plan").then((r) => r.data),
  drill: (data) => api.post("/coach/drill", data).then((r) => r.data),
  performance: (attemptId, lang = "en") => api.post(`/ai/performance-coach/${attemptId}`, null, { params: { lang } }).then((r) => r.data),
  studyPlan: (attemptId) => api.post(`/ai/study-plan/${attemptId}`).then((r) => r.data),
  planToggle: (attemptId, key, done) => api.post(`/ai/study-plan/${attemptId}/toggle`, { key, done }).then((r) => r.data),
};

export const explainApi = {
  generate: (qid, save = false) => api.post(`/ai/explain/${qid}`, null, { params: { save } }).then((r) => r.data),
};

export const proctorApi = {
  event: (attemptId, type) => api.post(`/attempts/${attemptId}/proctor-event`, { type }).then((r) => r.data),
  adminList: (test_id) => api.get("/admin/proctoring", { params: test_id ? { test_id } : {} }).then((r) => r.data),
};

export const reviewApi = {
  stats: () => api.get("/reviews/stats").then((r) => r.data),
  due: (limit = 20) => api.get("/reviews/due", { params: { limit } }).then((r) => r.data),
  grade: (data) => api.post("/reviews/grade", data).then((r) => r.data),
};

export const adaptiveApi = {
  start: (data) => api.post("/adaptive/start", data).then((r) => r.data),
  answer: (data) => api.post("/adaptive/answer", data).then((r) => r.data),
};

export const battleApi = {
  create: (data) => api.post("/battles/create", data).then((r) => r.data),
  join: (data) => api.post("/battles/join", data).then((r) => r.data),
  start: (id) => api.post(`/battles/${id}/start`).then((r) => r.data),
  state: (id) => api.get(`/battles/${id}`).then((r) => r.data),
  answer: (id, data) => api.post(`/battles/${id}/answer`, data).then((r) => r.data),
};

export const usersApi = {
  list: (role) => api.get("/users", { params: role ? { role } : {} }).then((r) => r.data),
  get: (id) => api.get(`/users/${id}`).then((r) => r.data),
  purgeAll: () => api.post("/admin/users/purge", { confirm: "DELETE ALL USERS" }).then((r) => r.data),
};

export const rewardsApi = {
  me: () => api.get("/rewards/me").then((r) => r.data),
  useFreeze: () => api.post("/rewards/freeze/use").then((r) => r.data),
};

export const storeApi = {
  get: () => api.get("/store").then((r) => r.data),
  buy: (item_id) => api.post("/store/buy", { item_id }).then((r) => r.data),
};

export const percentileApi = {  get: (testId, score) => api.get(`/analytics/test-percentile/${testId}`, { params: { score } }).then((r) => r.data),
};

export const duelsApi = {
  create: (data) => api.post("/duels", data).then((r) => r.data),
  get: (code) => api.get(`/duels/${code}`).then((r) => r.data),
  complete: (code, attempt_id) => api.post(`/duels/${code}/complete`, { attempt_id }).then((r) => r.data),
};

export const wrongRetestApi = {
  create: (attemptId) => api.post(`/attempts/${attemptId}/wrong-retest`).then((r) => r.data),
};

export const podcastsApi = {
  chapters: () => api.get("/podcasts/chapters").then((r) => r.data),
  script: (subject, chapter) => api.post("/podcasts/script", { subject, chapter }).then((r) => r.data),
};

export const digestApi = {
  weekly: (childId) => api.get(`/parent/digest/${childId}`).then((r) => r.data),
};

export const notificationsApi = {
  list: () => api.get("/notifications").then((r) => r.data),
  read: (id) => api.post(`/notifications/read/${id}`).then((r) => r.data),
};

export const announcementsApi = {
  list: () => api.get("/announcements").then((r) => r.data),
  create: (data) => api.post("/announcements", data).then((r) => r.data),
};

export const metaApi = {
  subjects: () => api.get("/meta/subjects").then((r) => r.data),
};

export const liveClassesApi = {
  list: (status) => api.get("/live-classes", { params: status ? { status_f: status } : {} }).then((r) => r.data),
  liveNow: () => api.get("/live-classes/live-now").then((r) => r.data),
  get: (id) => api.get(`/live-classes/${id}`).then((r) => r.data),
  create: (data) => api.post("/live-classes", data).then((r) => r.data),
  update: (id, data) => api.put(`/live-classes/${id}`, data).then((r) => r.data),
  remove: (id) => api.delete(`/live-classes/${id}`).then((r) => r.data),
  join: (id) => api.post(`/live-classes/${id}/join`).then((r) => r.data),
  attendance: (id) => api.get(`/live-classes/${id}/attendance`).then((r) => r.data),
  mark: (id, student_id, present) => api.post(`/live-classes/${id}/attendance/mark`, { student_id, present }).then((r) => r.data),
  csvUrl: (id) => `${API_BASE}/live-classes/${id}/attendance.csv`,
};
