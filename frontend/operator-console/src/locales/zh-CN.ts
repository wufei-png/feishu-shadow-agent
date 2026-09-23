import { copyFor } from "./copy";
import { chineseCatalogResources } from "../catalogPresentation";

export const zhCN = { ...copyFor(0), ...chineseCatalogResources() };
