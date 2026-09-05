"use client";
// 错误边界（FE-32）：画布数据畸形时显示占位符而非整页崩溃。
import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error) {
    // FE-32：控制台留 ErrorBoundary 标识，页面本身不白屏
    console.error("ErrorBoundary caught:", error);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-full items-center justify-center rounded-xl border border-dashed border-[#3a4250] font-mono text-sm tracking-widest text-pt-muted">
          数据格式异常
        </div>
      );
    }
    return this.props.children;
  }
}
