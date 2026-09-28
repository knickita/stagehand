using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Manipulate : MonoBehaviour
{
    GameObject axis;

    UIManager manager;

    Inspector inspector;
    // Start is called before the first frame update
    void Start()
    {
        axis = transform.Find("Axis").gameObject;
        manager = FindObjectOfType<UIManager>();
        inspector = FindObjectOfType<Inspector>();
    }

    void Update(){
        if (manager.selected.Count>0){
            axis.SetActive(true);
            SetCenter();
        }
        else{
            axis.SetActive(false);
        }
        Resize();
    }

    void SetCenter(){
        Vector3 center = Vector3.zero;
        foreach (GameObject go in manager.selected){
            center += go.transform.position;
        }
        center /= manager.selected.Count;
        transform.position = center;
    }

    void Resize()
    {
        float zoomFactor = Camera.main.orthographicSize / 5f; // Adjust the divisor as needed
        transform.localScale = Vector3.one * zoomFactor;
    }

    public void Move(Vector3 move){
        manager.MoveSelected(move);
        inspector.UpdateData();
    }
}
