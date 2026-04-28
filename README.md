# 📂 Exported Briefs — Multi-Agent Competitive Intelligence System

This repository contains **auto-generated company briefs** produced as part of a **Multi-Agent Competitive Intelligence System** built under the *Leveraging LLM Productivity for Improvement* coursework.

The system uses an agentic workflow to generate structured intelligence reports for selected companies and exports them as PDF artifacts.

---

## 📌 Overview

Each file in this directory represents a **generated intelligence brief** for a specific company. These briefs are created using a multi-agent pipeline that:

* Collects company-related data
* Processes and summarizes insights
* Structures outputs into standardized formats
* Exports final reports as PDFs

This aligns with modern **agentic AI workflows**, where multiple steps (retrieval, reasoning, generation) are orchestrated to produce high-quality outputs

---

## 📁 Directory Contents

```
exported_briefs/
│
├── AMD_20260427_223206.pdf
├── CapMetro_20260427_222556.pdf
├── Chuwi_20260427_224329.pdf
├── NVIDIA_20260427_221837.pdf
├── Tesla_20260427_221553.pdf
├── UiPath_20260427_223853.pdf
├── VMware_20260427_231144.pdf
│
├── README.md
├── LICENSE
└── .git/
```

### 📄 File Naming Convention

```
<CompanyName>_<YYYYMMDD>_<Timestamp>.pdf
```

* **CompanyName** → Target entity analyzed
* **Date** → Generation date
* **Timestamp** → Exact generation time

---

## 🧠 How These Briefs Are Generated

The underlying system follows a **retrieval + generation pipeline**, similar to RAG-based architectures:

1. Retrieve relevant company data
2. Augment context with curated insights
3. Generate structured summaries using LLMs
4. Export formatted PDF reports

This approach ensures that outputs are **context-aware and grounded in retrieved information** rather than relying only on pretraining knowledge

---

## 🎯 Purpose

These briefs are designed for:

* Competitive intelligence analysis
* Quick company overviews
* Strategy and market research
* Supporting business decision-making

---

## ⚙️ System Context

This directory is part of a broader system with:

* **Supervisor–Worker agent architecture**
* Modular agent responsibilities (data collection, summarization, validation)
* Structured output pipelines
* Optional human-in-the-loop validation

---

## 🚀 Usage

You can:

* Open PDFs directly for insights
* Use them as inputs for further analysis
* Integrate into dashboards or presentations

---

## ⚠️ Notes

* These reports are **auto-generated** and may require validation for critical decisions
* Data freshness depends on retrieval sources used during generation
* File sizes are minimal because they are **text-based structured briefs**

---

## 📜 License

See the `LICENSE` file for details.
